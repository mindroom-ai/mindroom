"""Approval continuations retain provider failures without replaying approved tools.

The agent half runs on the owned loop, where a failed resume raises its provider error after the record lands.
The team half still runs through Agno's team continuation until the team runtime cutover.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.exceptions import ModelProviderError
from agno.models.response import ModelResponse
from agno.run.base import RunStatus as AgnoRunStatus
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput
from agno.team import Team
from agno.tools.function import Function

from mindroom.agent_loop.run import resume_agent, run_agent
from mindroom.approval.execution import _collect_agent_continuation
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history.request_messages import build_resume_request
from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.provider import TextDelta
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.providers.synthetic_model import SyntheticModel
from mindroom.teams import _collect_team_continuation, _TeamStreamPresentation
from mindroom.tool_system.events import CollectedStreamPresentation
from tests.agent_loop.helpers import collect, context, gated, spec
from tests.fakes.scripted_model import Await, ScriptedModel, calls, tool_call

pytestmark = pytest.mark.asyncio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.models.message import Message as AgnoMessage


async def _collect_team(events: AsyncIterator[object]) -> TeamRunOutput:
    return await _collect_team_continuation(
        events,
        _TeamStreamPresentation.new([], [], show_tool_calls=True),
        progress=None,
    )


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
            progress=None,
        )

    assert executed == [["echo", "hi"]]
    assert [record.status for record in stored] == [RunStatus.paused, RunStatus.error]
    assert stored[-1].run_id == paused.run_id
    assert [tool.result for tool in stored[-1].tools or ()] == ["approved effect completed"]
    assert presentation.final_text().endswith("Partial continuation.")


@pytest.mark.parametrize("terminal_output", [False, True])
@pytest.mark.parametrize(
    ("content", "additional_data", "error_type", "expected"),
    [
        ("provider connection lost", None, None, "provider connection lost"),
        (None, {"error": {"message": "provider connection lost"}}, None, "provider connection lost"),
        (None, None, "APITimeoutError", "type=APITimeoutError"),
        (None, None, None, "run failed without provider error details"),
    ],
)
async def test_team_continuation_preserves_error_and_drains_stream(
    *,
    terminal_output: bool,
    content: str | None,
    additional_data: dict[str, object] | None,
    error_type: str | None,
    expected: str,
) -> None:
    """An error keeps its details even with stale terminal text, and producer cleanup finishes."""
    drained = False

    async def events() -> AsyncIterator[object]:
        nonlocal drained
        yield TeamRunErrorEvent(content=content, additional_data=additional_data, error_type=error_type)
        if terminal_output:
            yield TeamRunOutput(status=AgnoRunStatus.error, content="Earlier partial answer")
        drained = True

    with pytest.raises(RuntimeError, match=expected):
        await _collect_team(events())

    assert drained


@pytest.mark.parametrize("field", ["content", "additional_data", "error_type", "error_id"])
async def test_team_continuation_redacts_credentials_from_preserved_errors(field: str) -> None:
    """Preserved errors are safe for the approval failure reply."""
    message = "provider failed: api_key=example-secret at https://provider.example/run?token=example-token"
    value = {"error": {"message": message}} if field == "additional_data" else message

    async def events() -> AsyncIterator[object]:
        yield TeamRunErrorEvent(**{field: value})

    with pytest.raises(RuntimeError, match="provider failed") as caught:
        await _collect_team(events())

    assert "example-secret" not in str(caught.value)
    assert "example-token" not in str(caught.value)
    assert "***redacted***" in str(caught.value)


async def test_team_continuation_keeps_success_after_child_error() -> None:
    """A handled child failure must not override a successful parent result."""
    terminal = TeamRunOutput(run_id="parent", status=AgnoRunStatus.completed, content="Recovered answer")

    async def events() -> AsyncIterator[object]:
        yield TeamRunErrorEvent(run_id="child", parent_run_id="parent", content="child failed")
        yield terminal

    assert await _collect_team(events()) is terminal


class _FailingContinuationModel(SyntheticModel):
    """Keep real tool selection and fail only the model request after approval."""

    async def ainvoke_stream(self, messages: list[AgnoMessage], **kwargs: object) -> AsyncIterator[ModelResponse]:
        del messages, kwargs
        yield ModelResponse(content="Partial continuation.")
        raise ModelProviderError(message="provider connection lost", model_name=self.name, model_id=self.id)


async def test_real_team_approval_failure_preserves_error_and_executes_tool_once(tmp_path: Path) -> None:
    """Agno's team continuation records the failure and never repeats the approved side effect."""
    executed: list[list[str]] = []

    def run_shell_command(args: list[str]) -> str:
        executed.append(args)
        return "approved effect completed"

    model = _FailingContinuationModel(id="synthetic", chars_per_second=0, tool_call_probability=1)
    tools = [Function(name="run_shell_command", entrypoint=run_shell_command, requires_confirmation=True)]
    db = SqliteDb(db_file=str(tmp_path / "approval.db"))
    actor = Team(model=model, members=[], tools=tools, db=db, telemetry=False)
    paused = await actor.arun("Execute the approved tool", session_id="session", stream=False)
    assert paused.status == AgnoRunStatus.paused
    assert executed == []
    requirement = (paused.requirements or [])[0]
    requirement.confirm()

    events = actor.acontinue_run(paused, stream=True, stream_events=True, yield_run_output=True)
    with pytest.raises(RuntimeError, match="provider connection lost"):
        await _collect_team(events)

    assert executed == [["echo", "hi"]]
    assert paused.run_id is not None
    persisted = await actor.aget_run_output(paused.run_id, session_id="session")
    assert persisted is not None
    assert persisted.status == AgnoRunStatus.error
    assert [tool.result for tool in persisted.tools or ()] == ["approved effect completed"]
