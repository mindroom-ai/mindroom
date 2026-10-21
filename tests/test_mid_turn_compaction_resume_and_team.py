"""Mid-turn compaction across approval resumes and team runs, still driven through Agno agents and teams.

The agent-run cases moved to the owned loop in ``test_mid_turn_compaction.py``; these wait for the approval
resume and team runtimes to move onto the owned loop.
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.db.sqlite import SqliteDb
from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.summary import SessionSummary
from agno.team import Team
from agno.tools.function import Function

from mindroom.agents import create_agent
from mindroom.config.agent import TeamConfig
from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.execution_preparation import _PreparedExecutionContext
from mindroom.history import agno_compat_message_builder
from mindroom.history.mid_turn_compaction import install_mid_turn_compaction
from mindroom.history.summary_message import is_compaction_summary
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import (
    CompactionOutcome,
    HistoryScope,
    PreparedHistoryState,
)
from mindroom.providers.synthetic_model import SyntheticModel
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.teams import TeamMode, _create_team_instance, prepare_materialized_team_execution
from mindroom.usage.storage import COMPACTED_REQUESTS_METADATA_KEY, project_usage
from tests.conftest import make_turn_context, seed_session
from tests.history_helpers import (
    FakeModel,
    RecordingCompactionLifecycle,
    _completed_run,
    _completed_team_run,
    _make_config,
    _session,
    _team_session,
    log_of,
    segments_of,
    summary_rows_of,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_TOOL_RESULT = "r" * 4000
# Metadata a follow-up after an approval resume seeds from the resumed run, including that run's folded usage.
_SEEDED_METADATA = {COMPACTED_REQUESTS_METADATA_KEY: [{"input_tokens": 10}], "room_topic": "kept"}


@pytest.fixture(autouse=True)
def _install_message_builder_patch() -> None:
    agno_compat_message_builder.apply_patch()


@dataclass
class _Request:
    roles: list[str]
    contents: list[str]
    messages: list[Message]


class _ToolLoopModel(SyntheticModel):
    """Call ``probe`` for ``tool_rounds`` requests, then answer; record every request."""

    def __init__(
        self,
        *,
        tool_rounds: int,
        calls_per_round: int = 1,
        usage: Callable[[int], MessageMetrics] | None = None,
        reasoning: str | None = None,
    ) -> None:
        super().__init__(id="test-model", name="test", provider="test")
        self.tool_rounds = tool_rounds
        self.calls_per_round = calls_per_round
        self.usage = usage
        self.reasoning = reasoning
        self.requests: list[_Request] = []

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:
        self.requests.append(
            _Request(
                roles=[message.role for message in messages],
                contents=[str(message.content) for message in messages],
                messages=list(messages),
            ),
        )
        number = len(self.requests)
        metrics = self.usage(number) if self.usage is not None else MessageMetrics()
        if number <= self.tool_rounds:
            return ModelResponse(
                content=f"step {number}",
                tool_calls=[
                    {
                        "id": f"call_{number}_{index}",
                        "type": "function",
                        "function": {"name": "probe", "arguments": "{}"},
                    }
                    for index in range(self.calls_per_round)
                ],
                reasoning_content=self.reasoning,
                response_usage=metrics,
            )
        return ModelResponse(content="done", response_usage=metrics)

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


def probe() -> str:
    """Return one large tool result."""
    return _TOOL_RESULT


class _PausingLoop(_ToolLoopModel):
    """A tool loop whose fifth response calls ``approve_me``, which pauses the run for approval."""

    async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
        response = await super().ainvoke(messages, **kwargs)
        if len(self.requests) == 5 and response.tool_calls:
            response.tool_calls[0]["function"]["name"] = "approve_me"
        return response


def _approval_tools() -> list[Any]:
    return [probe, Function(name="approve_me", entrypoint=lambda: "approved", requires_confirmation=True)]


@dataclass
class _SummaryCalls:
    inputs: list[str] = field(default_factory=list)
    fail: bool = False
    cancel: bool = False

    async def __call__(
        self,
        *,
        model: object,  # noqa: ARG002
        summary_input: str,
        summary_prompt: str,  # noqa: ARG002
        timeout_seconds: float,  # noqa: ARG002
        on_usage: Callable[[MessageMetrics], Any] | None = None,
    ) -> SessionSummary:
        self.inputs.append(summary_input)
        if on_usage is not None:
            await on_usage(MessageMetrics(input_tokens=7, output_tokens=3))
        if self.cancel:
            raise asyncio.CancelledError
        if self.fail:
            msg = "summary model unavailable"
            raise RuntimeError(msg)
        return SessionSummary(summary=f"SUMMARY-{len(self.inputs)}", updated_at=datetime.now(UTC))


@pytest.fixture
def summary_calls(monkeypatch: pytest.MonkeyPatch) -> _SummaryCalls:
    calls = _SummaryCalls()
    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", calls)
    monkeypatch.setattr(
        "mindroom.providers.model_loading.get_model_instance",
        lambda *_args, **_kwargs: FakeModel(id="summary-model", provider="fake"),
    )
    return calls


def _config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    context_window: int | None = 4000,
    replay_window_tokens: int | None = None,
    summary_context_window: int = 100_000,
    teams: dict[str, TeamConfig] | None = None,
) -> tuple[Config, RuntimePaths]:
    return _make_config(
        tmp_path,
        teams=teams,
        defaults_compaction=CompactionConfig(
            enabled=enabled,
            model="summary",
            reserve_tokens=500,
            replay_window_tokens=replay_window_tokens,
        ),
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=context_window),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=summary_context_window),
        },
    )


_TEAMS = {"test_team": TeamConfig(display_name="Test Team", role="Test team", agents=["test_agent"])}


def _seeded_team_storage(config: Config, runtime_paths: RuntimePaths) -> object:
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    runs = [_completed_team_run("run-1", team_id="test_team"), _completed_team_run("run-2", team_id="test_team")]
    seed_session(storage, _team_session("session-1", team_id="test_team", runs=runs))
    return storage


def _scoped_team(
    model: SyntheticModel,
    config: Config,
    runtime_paths: RuntimePaths,
    storage: object,
    *,
    members: list[Agent] | None = None,
    tools: list[Any] | None = None,
) -> Team:
    team = Team(
        id="test_team",
        name="test_team",
        model=model,
        members=members or [],
        tools=tools if tools is not None else [probe],
        db=storage,
        add_history_to_context=True,
        store_history_messages=False,
        add_session_summary_to_context=False,
        telemetry=False,
    )
    install_mid_turn_compaction(
        team,
        config=config,
        runtime_paths=runtime_paths,
        entity_name="test_team",
        model_name="default",
    )
    return team


def _agent(
    model: _ToolLoopModel,
    config: Config,
    runtime_paths: RuntimePaths,
    **kwargs: Any,  # noqa: ANN401
) -> Agent:
    agent = Agent(
        id="test_agent",
        model=model,
        tools=[probe],
        add_history_to_context=False,
        telemetry=False,
        **kwargs,
    )
    install_mid_turn_compaction(
        agent,
        config=config,
        runtime_paths=runtime_paths,
        entity_name="test_agent",
        model_name="default",
    )
    return agent


async def _run(
    agent: Agent,
    prompt: list[Message] | str,
    *,
    stream: bool = False,
    session_id: str | None = None,
) -> RunOutput:
    if not stream:
        output = await agent.arun(prompt, session_id=session_id)
        assert isinstance(output, RunOutput)
        return output
    final: RunOutput | None = None
    async for event in agent.arun(prompt, session_id=session_id, stream=True, yield_run_output=True):
        if isinstance(event, RunOutput):
            final = event
    assert final is not None
    return final


_run_in_session = partial(_run, session_id="session-1")


def _first_compacted(model: _ToolLoopModel) -> _Request:
    return next(request for request in model.requests if any(is_compaction_summary(m) for m in request.messages))


def _ends_with_prompt_then_summary(request: _Request, prompt: str = "Do the task") -> bool:
    """Whether a compacted request ends with its prompt and then the summary of the work done on it."""
    return request.contents[-2] == prompt and is_compaction_summary(request.messages[-1])


@pytest.mark.asyncio
async def test_folded_request_usage_survives_approval_resume(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)

    model = _PausingLoop(
        tool_rounds=9,
        usage=lambda number: MessageMetrics(input_tokens=1001 * number, output_tokens=1),
    )
    agent = _agent(model, config, paths, db=SqliteDb(db_file=str(tmp_path / "runs.db")))
    agent.tools = _approval_tools()
    paused = await agent.arun("Do the task", session_id="session")
    for requirement in paused.requirements or []:
        requirement.confirm()

    output = await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session")

    assert len(summary_calls.inputs) >= 2
    requests = project_usage(output.to_dict())["requests"]
    assert [request["metrics"]["input_tokens"] for request in requests] == [
        1001 * number for number in range(1, len(model.requests) + 1)
    ]


@pytest.mark.asyncio
async def test_resumed_run_does_not_anchor_on_its_paused_response(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path, context_window=1_000_000)
    model = _ToolLoopModel(
        tool_rounds=1,
        usage=lambda number: MessageMetrics(input_tokens=999_900 if number == 1 else 10),
    )
    agent = _agent(model, config, paths, db=InMemoryDb())
    agent.tools = [Function(name="probe", entrypoint=probe, requires_confirmation=True)]

    paused = await agent.arun("Do the task", session_id="session")
    assert paused.status == RunStatus.paused
    for requirement in paused.requirements or []:
        requirement.confirm()
    await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session")

    assert summary_calls.inputs == []
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_run_local_summary_survives_approval_resume(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)

    model = _PausingLoop(tool_rounds=6)
    agent = _agent(model, config, paths, db=SqliteDb(db_file=str(tmp_path / "runs.db")))
    agent.tools = _approval_tools()

    paused = await agent.arun("Do the task", session_id="session")
    assert paused.status == RunStatus.paused
    assert summary_calls.inputs
    for requirement in paused.requirements or []:
        requirement.confirm()
    model.requests.clear()
    await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session")

    resumed = model.requests[0]
    summaries = [message for message in resumed.messages if is_compaction_summary(message)]
    assert len(summaries) >= 1
    assert all(summary.from_history is False for summary in summaries)


@pytest.mark.asyncio
async def test_scoped_compaction_after_resume_keeps_the_run_local_summary(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)

    model = _PausingLoop(tool_rounds=6)
    agent = _agent(model, config, paths, db=SqliteDb(db_file=str(tmp_path / "runs.db")))
    agent.tools = _approval_tools()
    paused = await agent.arun("Do the task", session_id="session")
    assert paused.status == RunStatus.paused
    local_summaries = len(summary_calls.inputs)
    assert local_summaries
    for requirement in paused.requirements or []:
        requirement.confirm()

    # The resumed request replays stored history, so its next compaction is scoped.
    agent.add_history_to_context = True
    model.requests.clear()
    await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session")

    assert len(summary_calls.inputs) > local_summaries
    assert f"SUMMARY-{local_summaries}" in summary_calls.inputs[local_summaries]


def _factory_target(kind: str, config: Config, paths: RuntimePaths) -> Agent | Team:
    if kind == "agent":
        target: Agent | Team = create_agent("test_agent", config, paths, execution_identity=None)
    else:
        target = _create_team_instance(
            agents=[],
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=paths,
            team_display_name="Test Team",
            scope_context=None,
            execution_identity=None,
            model_name="default",
            configured_team_name="test_team",
        )
    target.tools = [probe]
    return target


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["team"])
async def test_factories_cap_a_request_before_compacting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
    kind: str,
) -> None:
    config, paths = _config(
        tmp_path,
        context_window=12_000,
        teams=_TEAMS,
    )
    # The fourth response reports a request far over the window, so only the fifth request would compact.
    model = _ToolLoopModel(
        tool_rounds=8,
        usage=lambda number: MessageMetrics(input_tokens=1_000_000 if number == 4 else 10, output_tokens=1),
    )

    def load_model(_config: Config, _paths: RuntimePaths, model_name: str = "default", **_kwargs: object) -> object:
        return model if model_name == "default" else FakeModel(id="summary-model", provider="fake")

    monkeypatch.setattr("mindroom.providers.model_loading.get_model_instance", load_model)
    target = _factory_target(kind, config, paths)
    # The cap allows the limit plus two requests, so it refuses the fifth.
    target.tool_call_limit = 2

    await target.arun("Do the task")

    assert len(model.requests) == 4
    assert summary_calls.inputs == []


@pytest.mark.asyncio
async def test_prepared_team_reply_posts_mid_turn_compaction_notices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    """The team reply binds its compaction notices, and its metadata leaves out the earlier run's fold records."""
    config, paths = _config(tmp_path, teams=_TEAMS)
    storage = _seeded_team_storage(config, paths)
    team = _scoped_team(_ToolLoopModel(tool_rounds=5), config, paths, storage)
    prepared = _PreparedExecutionContext(
        messages=(Message(role="user", content="Do the task"),),
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(),
    )
    monkeypatch.setattr("mindroom.teams.prepare_bound_team_run_context", AsyncMock(return_value=prepared))
    lifecycle = RecordingCompactionLifecycle()
    recorder = TurnRecorder(user_message="Do the task")

    prepared_execution = await prepare_materialized_team_execution(
        replace(make_turn_context("test_team", session_id="session-1"), matrix_run_metadata=_SEEDED_METADATA),
        scope_context=None,
        agents=[],
        team=team,
        message="Do the task",
        thread_history=[],
        config=config,
        runtime_paths=paths,
        runtime_model=config.resolve_runtime_model(entity_name="test_team"),
        response_sender_id=None,
        current_sender_id=None,
        configured_team_name="test_team",
        compaction_lifecycle=lifecycle,
        turn_recorder=recorder,
    )
    await team.arun("Do the task", session_id="session-1")

    assert summary_calls.inputs
    assert any(isinstance(event, CompactionOutcome) for event in lifecycle.events)
    assert "call_1_0" in recorder.folded_tool_call_ids
    assert prepared_execution.run_metadata is not None
    assert COMPACTED_REQUESTS_METADATA_KEY not in prepared_execution.run_metadata
    assert prepared_execution.run_metadata["room_topic"] == "kept"


@pytest.mark.asyncio
async def test_team_member_loop_compacts_run_locally(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    member_model = _ToolLoopModel(tool_rounds=5)
    member = _agent(member_model, config, paths)
    member.name = "Test Agent"
    team = Team(id="crew", name="Crew", model=_Leader("Do the task"), members=[member], telemetry=False)

    output = await team.arun("Coordinate the task")

    assert str(output.content).endswith("team done")
    assert summary_calls.inputs
    compacted = _first_compacted(member_model)
    assert [message.from_history for message in compacted.messages if is_compaction_summary(message)] == [False]


_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
_TEAM_SCOPE = HistoryScope(kind="team", scope_id="test_team")


def _scoped_agent(
    model: _ToolLoopModel,
    config: Config,
    runtime_paths: RuntimePaths,
    storage: object,
    **kwargs: Any,  # noqa: ANN401
) -> Agent:
    agent = _agent(model, config, runtime_paths, db=storage, **kwargs)
    agent.add_history_to_context = True
    agent.num_history_runs = None
    agent.store_history_messages = False
    return agent


def _seeded_storage(config: Config, runtime_paths: RuntimePaths) -> object:
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    seed_session(
        storage,
        _session(
            "session-1",
            runs=[
                _completed_run(
                    "run-1",
                    messages=[Message(role="user", content="q1"), Message(role="assistant", content="a1")],
                ),
                _completed_run(
                    "run-2",
                    messages=[Message(role="user", content="q2"), Message(role="assistant", content="a2")],
                ),
            ],
        ),
    )
    return storage


@pytest.mark.asyncio
@pytest.mark.usefixtures("summary_calls")
@pytest.mark.parametrize("stream", [False, True])
async def test_long_single_team_turn_compacts_mid_turn_and_continues(
    tmp_path: Path,
    *,
    stream: bool,
) -> None:
    config, paths = _config(tmp_path, teams=_TEAMS)
    storage = _seeded_team_storage(config, paths)
    model = _ToolLoopModel(tool_rounds=5)
    team = _scoped_team(model, config, paths, storage)

    if stream:
        outputs = [
            event
            async for event in team.arun("Do the task", session_id="session-1", stream=True, yield_run_output=True)
            if isinstance(event, TeamRunOutput)
        ]
        output = outputs[-1]
    else:
        output = await team.arun("Do the task", session_id="session-1")
    assert isinstance(output, TeamRunOutput)

    assert str(output.content).endswith("done")
    compacted = _first_compacted(model)
    summaries = [message for message in compacted.messages if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert summaries[0].from_history is True
    assert _ends_with_prompt_then_summary(compacted)
    assert not any("team question" in content or "team answer" in content for content in compacted.contents)
    rows = summary_rows_of(storage, _TEAM_SCOPE.key)
    assert log_of(storage, _TEAM_SCOPE.key) == [
        "run:run-1",
        "run:run-2",
        *(f"row:{row.summary}" for row in rows),
        f"run:{output.run_id}",
    ]
    assert {run_id for run_id, _segment in segments_of(storage, _TEAM_SCOPE.key)} == {output.run_id}
    stored = storage.load_session("session-1", "team")
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == rows[-1].summary


class _DelegateThenLoop(_ToolLoopModel):
    """Delegate to the member first, then run the tool loop."""

    async def ainvoke(self, messages: list[Message], **kwargs: object) -> ModelResponse:
        response = await super().ainvoke(messages, **kwargs)
        if len(self.requests) == 1:
            response.tool_calls = [
                {
                    "id": "delegate_1",
                    "type": "function",
                    "function": {
                        "name": "delegate_task_to_member",
                        "arguments": '{"member_id": "helper", "task": "Do the subtask"}',
                    },
                },
            ]
        return response


@pytest.mark.asyncio
async def test_scoped_team_compaction_keeps_the_turns_member_runs(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path, teams=_TEAMS)
    storage = create_session_storage("test_agent", config, paths, execution_identity=None)
    member = Agent(id="helper", name="helper", model=_ToolLoopModel(tool_rounds=0), telemetry=False)
    team = _scoped_team(_DelegateThenLoop(tool_rounds=6), config, paths, storage, members=[member])

    output = await team.arun("Do the task", session_id="session-1")

    assert isinstance(output, TeamRunOutput)
    assert str(output.content).endswith("done")
    assert summary_calls.inputs
    stored = storage.load_session("session-1", "team")
    assert stored is not None
    assert any(run.parent_run_id == output.run_id for run in stored.runs or [])


@pytest.mark.asyncio
async def test_approval_resume_after_mid_turn_compaction(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)

    model = _PausingLoop(tool_rounds=6)
    agent = _scoped_agent(model, config, paths, storage)
    agent.tools = _approval_tools()

    paused = await agent.arun("Do the task", session_id="session-1")
    assert paused.status == RunStatus.paused
    assert summary_calls.inputs
    paused_request = model.requests[-1]
    for requirement in paused.requirements or []:
        requirement.confirm()
    model.requests.clear()
    await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session-1")

    resumed = model.requests[0]
    summaries = [message for message in resumed.messages if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert summaries[0].content == next(m.content for m in paused_request.messages if is_compaction_summary(m))
    assert resumed.contents[: len(paused_request.contents)] == paused_request.contents


@pytest.mark.asyncio
async def test_compaction_after_an_approval_resume_reaches_the_stored_session(
    tmp_path: Path,
    summary_calls: _SummaryCalls,
) -> None:
    config, paths = _config(tmp_path)
    storage = _seeded_storage(config, paths)

    model = _PausingLoop(tool_rounds=12)
    agent = _scoped_agent(model, config, paths, storage)
    agent.tools = _approval_tools()
    paused = await agent.arun("Do the task", session_id="session-1")
    assert paused.status == RunStatus.paused
    compactions_before_resume = len(summary_calls.inputs)
    for requirement in paused.requirements or []:
        requirement.confirm()

    await agent.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session-1")

    assert len(summary_calls.inputs) > compactions_before_resume
    stored = storage.load_session("session-1", "agent")
    rows = summary_rows_of(storage, _SCOPE.key)
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == rows[-1].summary
    # The completed run sorts after every fold of both legs, and the next reply replays only it after the summary.
    assert log_of(storage, _SCOPE.key) == [
        "run:run-1",
        "run:run-2",
        *(f"row:{row.summary}" for row in rows),
        f"run:{paused.run_id}",
    ]
    assert [run.run_id for run in stored.runs or []] == [paused.run_id]


@pytest.mark.asyncio
async def test_team_approval_resume_after_mid_turn_compaction(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path, teams=_TEAMS)
    storage = _seeded_team_storage(config, paths)

    model = _PausingLoop(tool_rounds=6)
    team = _scoped_team(model, config, paths, storage, tools=_approval_tools())

    paused = await team.arun("Do the task", session_id="session-1")
    assert paused.status == RunStatus.paused
    assert summary_calls.inputs
    paused_request = model.requests[-1]
    for requirement in paused.requirements or []:
        requirement.confirm()
    model.requests.clear()
    await team.acontinue_run(run_id=paused.run_id, requirements=paused.requirements, session_id="session-1")

    resumed = model.requests[0]
    summaries = [message for message in resumed.messages if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert summaries[0].content == next(m.content for m in paused_request.messages if is_compaction_summary(m))
    stored = storage.load_session("session-1", "team")
    rows = summary_rows_of(storage, _TEAM_SCOPE.key)
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.summary == rows[-1].summary


class _Leader(SyntheticModel):
    """Delegate each of ``tasks`` to ``test_agent`` in one response, then answer."""

    def __init__(self, *tasks: str) -> None:
        super().__init__(id="leader-model", name="leader", provider="test")
        self.tasks = tasks
        self.requests = 0

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:  # noqa: ARG002
        self.requests += 1
        if self.requests > 1:
            return ModelResponse(content="team done")
        return ModelResponse(
            tool_calls=[
                {
                    "id": f"delegate_{index}",
                    "type": "function",
                    "function": {
                        "name": "delegate_task_to_member",
                        "arguments": json.dumps({"member_id": "test_agent", "task": task}),
                    },
                }
                for index, task in enumerate(self.tasks)
            ],
        )

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


class _PerTaskLoop(SyntheticModel):
    """Call ``probe`` a fixed number of times per delegated task, interleaving tasks, then answer."""

    def __init__(self, *, tool_rounds: int) -> None:
        super().__init__(id="test-model", name="test", provider="test")
        self.tool_rounds = tool_rounds
        self.calls: dict[str, int] = {}
        self.requests: list[tuple[str, list[Message]]] = []

    async def ainvoke(self, messages: list[Message], **_kwargs: object) -> ModelResponse:
        task = next(name for name in ("Task A", "Task B") if any(name in str(message.content) for message in messages))
        self.requests.append((task, list(messages)))
        await asyncio.sleep(0)
        self.calls[task] = self.calls.get(task, 0) + 1
        if self.calls[task] <= self.tool_rounds:
            call_id = f"call_{task[-1]}_{self.calls[task]}"
            return ModelResponse(
                tool_calls=[{"id": call_id, "type": "function", "function": {"name": "probe", "arguments": "{}"}}],
            )
        return ModelResponse(content=f"{task} done")

    async def ainvoke_stream(self, messages: list[Message], **kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(messages, **kwargs)


@pytest.mark.asyncio
async def test_parallel_delegations_to_one_member_both_compact(tmp_path: Path, summary_calls: _SummaryCalls) -> None:
    config, paths = _config(tmp_path)
    member_model = _PerTaskLoop(tool_rounds=6)
    member = Agent(id="test_agent", name="Test Agent", model=member_model, tools=[probe], telemetry=False)
    install_mid_turn_compaction(
        member,
        config=config,
        runtime_paths=paths,
        entity_name="test_agent",
        model_name="default",
    )
    team = Team(id="crew", name="Crew", model=_Leader("Task A", "Task B"), members=[member], telemetry=False)

    output = await team.arun("Coordinate the tasks")

    assert str(output.content).endswith("team done")
    assert summary_calls.inputs
    for task in ("Task A", "Task B"):
        compacted = [
            messages
            for name, messages in member_model.requests
            if name == task and any(is_compaction_summary(m) for m in messages)
        ]
        assert compacted, task
