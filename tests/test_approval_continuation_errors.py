"""Approval continuations retain provider failures without replaying approved tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.exceptions import ModelProviderError
from agno.models.response import ModelResponse
from agno.run.agent import RunErrorEvent, RunOutput
from agno.run.base import RunStatus
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput
from agno.team import Team
from agno.tools.function import Function

from mindroom.approval_execution import _collect_agent_continuation
from mindroom.synthetic_model import SyntheticModel
from mindroom.teams import _collect_team_continuation, _TeamStreamPresentation
from mindroom.tool_system.events import CollectedStreamPresentation

pytestmark = pytest.mark.asyncio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.models.message import Message


async def _collect(events: AsyncIterator[object], *, team: bool) -> RunOutput | TeamRunOutput:
    if team:
        return await _collect_team_continuation(events, _TeamStreamPresentation.new([], [], show_tool_calls=True))
    return await _collect_agent_continuation(events, CollectedStreamPresentation(show_tool_calls=True))


@pytest.mark.parametrize("team", [False, True])
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
async def test_continuation_preserves_error_and_drains_stream(
    *,
    team: bool,
    terminal_output: bool,
    content: str | None,
    additional_data: dict[str, object] | None,
    error_type: str | None,
    expected: str,
) -> None:
    """An error keeps its details even with stale terminal text, and producer cleanup finishes."""
    error_class = TeamRunErrorEvent if team else RunErrorEvent
    output_class = TeamRunOutput if team else RunOutput
    drained = False

    async def events() -> AsyncIterator[object]:
        nonlocal drained
        yield error_class(content=content, additional_data=additional_data, error_type=error_type)
        if terminal_output:
            yield output_class(status=RunStatus.error, content="Earlier partial answer")
        drained = True

    with pytest.raises(RuntimeError, match=expected):
        await _collect(events(), team=team)

    assert drained


@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("field", ["content", "additional_data", "error_type", "error_id"])
async def test_continuation_redacts_credentials_from_preserved_errors(*, team: bool, field: str) -> None:
    """Preserved errors are safe for the approval failure reply."""
    error_class = TeamRunErrorEvent if team else RunErrorEvent
    message = "provider failed: api_key=example-secret at https://provider.example/run?token=example-token"
    value = {"error": {"message": message}} if field == "additional_data" else message

    async def events() -> AsyncIterator[object]:
        yield error_class(**{field: value})

    with pytest.raises(RuntimeError, match="provider failed") as caught:
        await _collect(events(), team=team)

    assert "example-secret" not in str(caught.value)
    assert "example-token" not in str(caught.value)
    assert "***redacted***" in str(caught.value)


@pytest.mark.parametrize("team", [False, True])
async def test_continuation_keeps_success_after_child_error(*, team: bool) -> None:
    """A handled child failure must not override a successful parent result."""
    error_class = TeamRunErrorEvent if team else RunErrorEvent
    output_class = TeamRunOutput if team else RunOutput
    terminal = output_class(run_id="parent", status=RunStatus.completed, content="Recovered answer")

    async def events() -> AsyncIterator[object]:
        yield error_class(run_id="child", parent_run_id="parent", content="child failed")
        yield terminal

    assert await _collect(events(), team=team) is terminal


class _FailingContinuationModel(SyntheticModel):
    """Keep real tool selection and fail only the model request after approval."""

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        del messages, kwargs
        yield ModelResponse(content="Partial continuation.")
        raise ModelProviderError(message="provider connection lost", model_name=self.name, model_id=self.id)


@pytest.mark.parametrize("team", [False, True])
async def test_real_approval_failure_preserves_error_and_executes_tool_once(tmp_path: Path, *, team: bool) -> None:
    """Real Agno approval execution records failure and never repeats the approved side effect."""
    executed: list[list[str]] = []

    def run_shell_command(args: list[str]) -> str:
        executed.append(args)
        return "approved effect completed"

    model = _FailingContinuationModel(id="synthetic", chars_per_second=0, tool_call_probability=1)
    tools = [Function(name="run_shell_command", entrypoint=run_shell_command, requires_confirmation=True)]
    db = SqliteDb(db_file=str(tmp_path / "approval.db"))
    actor = (
        Team(model=model, members=[], tools=tools, db=db, telemetry=False)
        if team
        else Agent(model=model, tools=tools, db=db, telemetry=False)
    )
    paused = await actor.arun("Execute the approved tool", session_id="session", stream=False)
    assert paused.status == RunStatus.paused
    assert executed == []
    requirement = (paused.requirements or [])[0]
    requirement.confirm()

    events = actor.acontinue_run(paused, stream=True, stream_events=True, yield_run_output=True)
    with pytest.raises(RuntimeError, match="provider connection lost"):
        await _collect(events, team=team)

    assert executed == [["echo", "hi"]]
    assert paused.run_id is not None
    persisted = await actor.aget_run_output(paused.run_id, session_id="session")
    assert persisted is not None
    assert persisted.status == RunStatus.error
    assert [tool.result for tool in persisted.tools or ()] == ["approved effect completed"]
