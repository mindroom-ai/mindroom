"""Mid-turn compaction across approval resumes and team runs, on the owned loop.

The approval cases run one agent turn through ``run_agent`` under the hooks a reply installs, pause it on a gated
call, and resume it through ``ResumeRunner`` as a saved approval does. The team cases drive the ``squad`` team of
``tests/team_turn_helpers.py`` through the team envelope, where the leader's and each member run's hooks compact
their own loops.
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from agno.tools import Toolkit

from mindroom import agents
from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.approval.resume import ResumeDecisions, ResumeRunner
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.main import Config
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history.replay import configured_replay_plan
from mindroom.history.request_messages import TurnMessages, build_request, replayed_history
from mindroom.history.storage import new_scope_session
from mindroom.history.summary_message import is_compaction_summary
from mindroom.history.types import CompactionOutcome
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import MessageMetrics
from mindroom.loop_hooks import agent_loop_hooks
from mindroom.response_turn import CompletedApprovalRun, ResponsePausedForApproval
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.store import persist_to
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.metadata import TOOL_METADATA, TOOL_REGISTRY, ToolCategory
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.usage.storage import COMPACTED_REQUESTS_METADATA_KEY, project_usage
from tests.conftest import bind_runtime_paths, seed_session, test_runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.fakes.summary_model import summary_model
from tests.history_helpers import (
    RecordingCompactionLifecycle,
    _completed_team_run,
    _team_session,
    log_of,
    segments_of,
    summary_rows_of,
)
from tests.team_turn_helpers import (  # noqa: F401 - squad_probe is a pytest fixture
    SESSION_ID,
    TEAM_SCOPE,
    ProbeToolkit,
    TeamTurn,
    member_approval,
    squad_probe,
    squad_turn,
)
from tests.test_mid_turn_compaction import _SCOPE, _TOOL_RESULT, _seeded_storage, _SummaryCalls, _ToolLoopModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelRequest, StreamEvent
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from tests.fakes.scripted_model import ScriptStep

# Metadata a follow-up after an approval resume seeds from the resumed run, including that run's folded usage.
_SEEDED_METADATA = {COMPACTED_REQUESTS_METADATA_KEY: [{"input_tokens": 10}], "room_topic": "kept"}
RESUME_PROBE_TOOL = "compaction_resume_probe"


@pytest.fixture
def summary_calls(monkeypatch: pytest.MonkeyPatch) -> _SummaryCalls:
    calls = _SummaryCalls()
    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", calls)
    monkeypatch.setattr("mindroom.history.compaction.build_model_provider", lambda *_args: summary_model())
    return calls


# ---------------------------------------------------------------------------
# Approval resumes
# ---------------------------------------------------------------------------


class _ResumeProbeToolkit(Toolkit):
    """``probe`` returns one large result; ``approve_me`` waits for the requester under the test's approval rule."""

    def __init__(self) -> None:
        super().__init__(name="probes", tools=[self.probe, self.approve_me])

    def probe(self) -> str:
        """Return one large tool result."""
        return _TOOL_RESULT

    def approve_me(self) -> str:
        """Return once the requester approved the call."""
        return "approved"


@pytest.fixture
def resume_probe() -> Iterator[None]:
    """Register the resume probe toolkit for the test and restore the registries afterwards."""
    original_registry = TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()

    @register_tool_with_metadata(
        name=RESUME_PROBE_TOOL,
        file_access=ToolFileAccess.NONE,
        display_name="Compaction Resume Probe",
        description="Fills a tool loop and pauses it for approval.",
        category=ToolCategory.DEVELOPMENT,
    )
    def probe_factory() -> type[_ResumeProbeToolkit]:
        return _ResumeProbeToolkit

    try:
        yield
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)


class _PausingLoop(_ToolLoopModel):
    """A tool loop whose fifth response calls ``approve_me``, which pauses the run for approval."""

    def respond(self, number: int, request: ModelRequest) -> Message:
        message = super().respond(number, request)
        if number == 5 and message.tool_calls:
            message.tool_calls[0]["function"]["name"] = "approve_me"
        return message


@dataclass
class _Resumable:
    """The test agent as one reply builds it, with the store its runs and resumes share.

    ``replays_history`` puts the stored runs and the scope summary in the first request and binds its compaction
    to the scope, as a reply that replays its history does; a resume always replays under the configured policy.
    """

    build: AgentBuild
    model: _ToolLoopModel
    config: Config
    runtime_paths: RuntimePaths
    store: ConversationStore
    session: ConversationSession
    replays_history: bool

    def runner(self) -> ResumeRunner:
        """Return the resume as a saved approval runs it: on the session as the store holds it after the pause."""
        session = self.store.load_session("session-1", "agent")
        assert session is not None
        return ResumeRunner(
            build=self.build,
            config=self.config,
            runtime_paths=self.runtime_paths,
            agent_name="test_agent",
            model_name="default",
            store=self.store,
            session=session,
            scope=_SCOPE,
            user_id=None,
        )


def _resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: _ToolLoopModel,
    *,
    context_window: int | None = 4000,
    replays_history: bool = False,
) -> _Resumable:
    authored: dict[str, Any] = {
        "agents": {"test_agent": {"display_name": "Test Agent", "tools": [RESUME_PROBE_TOOL]}},
        "defaults": {"tools": [], "compaction": {"model": "summary", "reserve_tokens": 500}},
        "memory": {"backend": "none"},
        "models": {
            "default": {"provider": "openai", "id": "test-model", "context_window": context_window},
            "summary": {"provider": "openai", "id": "summary-model", "context_window": 100_000},
        },
        "tool_approval": {"rules": [{"match": "approve_me", "action": "require_approval"}]},
        "timezone": "UTC",
    }
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config.model_validate(authored), paths)
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: model)
    store = (
        _seeded_storage(config, paths)
        if replays_history
        else create_session_storage("test_agent", config, paths, execution_identity=None)
    )
    build = agents.create_agent(
        "test_agent",
        config,
        paths,
        execution_identity=None,
        history_storage=store,
        supports_native_tool_approval=True,
    )
    session = store.load_session("session-1", "agent") or new_scope_session(
        session_id="session-1",
        scope_id="test_agent",
        is_team=False,
    )
    return _Resumable(build, model, config, paths, store, session, replays_history)


async def _pause(agent: _Resumable) -> RunRecord:
    """Run the first leg of a turn as a reply does and return its stored ``PAUSED`` record."""
    build = agent.build
    turn = TurnMessages(prompt=Message(role="user", content="Do the task"))
    plan = configured_replay_plan(history_settings=build.history_settings, estimated_tokens=0)
    history, summary = (
        replayed_history(agent.session, _SCOPE, build.history_settings, plan) if agent.replays_history else ([], None)
    )
    events = run_agent(
        replace(build.spec, system_message=prompt_for_turn(build)),
        build_request(system=(), summary=summary, history=history, turn=turn),
        ctx=RunContext(run_id=str(uuid4()), session_id="session-1", session_state=agent.session.session_state),
        hooks=agent_loop_hooks(
            build,
            config=agent.config,
            runtime_paths=agent.runtime_paths,
            agent_name="test_agent",
            model_name="default",
            store=agent.store,
            session=agent.session,
            scope=_SCOPE,
            scoped=agent.replays_history,
            native_compaction=None,
            prompt_id=turn.prompt.id,
        ),
        persist=persist_to(agent.store, agent.session),
        request_template=replace(build.request_template, stream=False),
    )
    finished = [event async for event in events][-1]
    assert isinstance(finished, RunFinished)
    assert isinstance(finished.record, RunRecord)
    assert finished.record.status is RunStatus.paused
    return finished.record


def _approved(paused: RunRecord) -> ResumeDecisions:
    """Approve every call the paused run waits for, as the requester's cards would."""
    return ResumeDecisions(
        approved=tuple(
            ApprovalCall(
                tool_call_id=str(tool.tool_call_id),
                tool_name=tool.tool_name,
                invoking_agent="test_agent",
                toolkit_name=RESUME_PROBE_TOOL,
                expires_at_ns=2**62,
                arguments_digest=approval_arguments_digest(tool.tool_args),
            )
            for tool in paused.tools or ()
            if tool.requires_confirmation
        ),
    )


async def _resume(agent: _Resumable, paused: RunRecord) -> RunRecord:
    return await agent.runner().continue_run(paused, _approved(paused), None)


def _summaries(messages: Sequence[Message]) -> list[Message]:
    return [message for message in messages if is_compaction_summary(message)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("resume_probe")
async def test_folded_request_usage_survives_approval_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    model = _PausingLoop(
        tool_rounds=9,
        usage=lambda number: MessageMetrics(input_tokens=1001 * number, output_tokens=1),
    )
    agent = _resumable(tmp_path, monkeypatch, model)

    paused = await _pause(agent)
    record = await _resume(agent, paused)

    assert record.status is RunStatus.completed
    assert len(summary_calls.inputs) >= 2
    requests = project_usage(record.to_dict())["requests"]
    assert [request["metrics"]["input_tokens"] for request in requests] == [
        1001 * number for number in range(1, len(model.requests) + 1)
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("resume_probe")
async def test_resumed_run_does_not_anchor_on_its_paused_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    """The response that paused reported a request near the window; the resume sizes its own request afresh."""
    model = _PausingLoop(
        tool_rounds=5,
        usage=lambda number: MessageMetrics(input_tokens=999_900 if number == 5 else 10),
    )
    agent = _resumable(tmp_path, monkeypatch, model, context_window=1_000_000)

    paused = await _pause(agent)
    await _resume(agent, paused)

    assert summary_calls.inputs == []
    assert len(model.requests) == 6


@pytest.mark.asyncio
@pytest.mark.usefixtures("resume_probe")
async def test_run_local_summary_survives_approval_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    model = _PausingLoop(tool_rounds=6)
    agent = _resumable(tmp_path, monkeypatch, model)

    paused = await _pause(agent)
    assert summary_calls.inputs
    before = len(model.requests)
    await _resume(agent, paused)

    resumed = model.requests[before]
    summaries = _summaries(resumed.messages)
    assert len(summaries) >= 1
    assert all(summary.from_history is False for summary in summaries)


@pytest.mark.asyncio
@pytest.mark.usefixtures("resume_probe")
async def test_scoped_compaction_after_resume_keeps_the_run_local_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    """A run-local summary of the first leg is part of what the resume's scoped compaction summarizes."""
    model = _PausingLoop(tool_rounds=10)
    agent = _resumable(tmp_path, monkeypatch, model)

    paused = await _pause(agent)
    local_summaries = len(summary_calls.inputs)
    assert local_summaries
    # The resume replays the scope's stored history, so its compactions fold into the scope.
    await _resume(agent, paused)

    assert len(summary_calls.inputs) > local_summaries
    assert f"SUMMARY-{local_summaries}" in summary_calls.inputs[local_summaries]
    # The first leg's folds were run-local; only the resume's folds reached the scope.
    rows = summary_rows_of(agent.store, _SCOPE.key)
    assert [row.summary for row in rows] == [
        f"SUMMARY-{number}" for number in range(local_summaries + 1, len(summary_calls.inputs) + 1)
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("resume_probe")
async def test_approval_resume_after_mid_turn_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    """The resumed request carries the summary the paused request saw, once, behind the same prefix."""
    model = _PausingLoop(tool_rounds=6)
    agent = _resumable(tmp_path, monkeypatch, model, replays_history=True)

    paused = await _pause(agent)
    assert summary_calls.inputs
    paused_request = model.requests[-1]
    before = len(model.requests)
    await _resume(agent, paused)

    resumed = model.requests[before]
    [summary] = _summaries(resumed.messages)
    [paused_summary] = _summaries(paused_request.messages)
    assert summary.content == paused_summary.content
    paused_contents = [str(message.content) for message in paused_request.messages]
    assert [str(message.content) for message in resumed.messages][: len(paused_contents)] == paused_contents


@pytest.mark.asyncio
@pytest.mark.usefixtures("resume_probe")
async def test_compaction_after_an_approval_resume_reaches_the_stored_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    model = _PausingLoop(tool_rounds=12)
    agent = _resumable(tmp_path, monkeypatch, model, replays_history=True)

    paused = await _pause(agent)
    compactions_before_resume = len(summary_calls.inputs)
    await _resume(agent, paused)

    assert len(summary_calls.inputs) > compactions_before_resume
    stored = agent.store.load_session("session-1", "agent")
    rows = summary_rows_of(agent.store, _SCOPE.key)
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.text == rows[-1].summary
    # The completed run sorts after every fold of both legs, and the next reply replays only it after the summary.
    assert log_of(agent.store, _SCOPE.key) == [
        "run:run-1",
        "run:run-2",
        *(f"row:{row.summary}" for row in rows),
        f"run:{paused.run_id}",
    ]
    assert [run.run_id for run in stored.runs] == [paused.run_id]


# ---------------------------------------------------------------------------
# Team turns
# ---------------------------------------------------------------------------


def _seed_team_history(team: TeamTurn) -> None:
    runs = [_completed_team_run("past-1", team_id="squad"), _completed_team_run("past-2", team_id="squad")]
    seed_session(team.storage, _team_session(SESSION_ID, team_id="squad", runs=runs))


def _big_answers(count: int) -> list[list[ScriptStep]]:
    """Return ``count`` member turns whose answers each fill a quarter of a 4,000-token window."""
    return [answer(_TOOL_RESULT) for _ in range(count)]


def _delegation(call_id: str, member_id: str, task: str, metrics: MessageMetrics | None = None) -> list[ScriptStep]:
    """Return a leader turn that delegates ``task``; without ``metrics`` the next request is sized by estimate."""
    return calls(tool_call("delegate_task_to_member", call_id, member_id=member_id, task=task), metrics=metrics)


def _delegations(count: int, member_id: str = "writer") -> list[list[ScriptStep]]:
    return [_delegation(f"call-{number}", member_id, f"Part {number}") for number in range(1, count + 1)]


def _first_compacted(model: ScriptedModel) -> ModelRequest:
    return next(request for request in model.requests if _summaries(request.messages))


def _contents(request: ModelRequest) -> list[str]:
    return [str(message.content) for message in request.messages]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.usefixtures("squad_probe")
async def test_long_single_team_turn_compacts_mid_turn_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
    *,
    stream: bool,
) -> None:
    """The leader's delegation loop outgrows the window; the fold replaces the replayed history and the turn so far."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[*_delegations(5), answer("All parts done")],
        writer=_big_answers(5),
        context_window=4000,
    )
    _seed_team_history(team)

    if stream:
        chunks = await team.stream()
        text = chunks[-1].content
    else:
        text = await team.respond()

    assert text.endswith("All parts done")
    assert summary_calls.inputs
    compacted = _first_compacted(team.models["default"])
    [summary] = _summaries(compacted.messages)
    assert summary.from_history is True
    contents = _contents(compacted)
    # The fold leaves the turn's prompt in place and puts the summary of everything else after it.
    assert "Hello" in contents[contents.index(str(summary.content)) - 1]
    assert not any("team question" in content or "team answer" in content for content in contents)
    record, members = team.stored_runs()
    rows = summary_rows_of(team.storage, TEAM_SCOPE.key, SESSION_ID)
    assert log_of(team.storage, TEAM_SCOPE.key, SESSION_ID) == [
        "run:past-1",
        "run:past-2",
        *(f"row:{row.summary}" for row in rows),
        f"run:{record.run_id}",
    ]
    assert {run_id for run_id, _segment in segments_of(team.storage, TEAM_SCOPE.key, SESSION_ID)} == {record.run_id}
    stored = team.storage.load_session(SESSION_ID, "team")
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.text == rows[-1].summary
    # The turn's member runs stay stored under the team run the fold summarized.
    assert len(members) == 5
    assert {member.parent_run_id for member in members} == {record.run_id}


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_prepared_team_reply_posts_mid_turn_compaction_notices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    """The team reply binds its compaction notices and recorder, and its metadata leaves out the earlier run's folds."""
    # Each response reports a request 1,200 tokens larger than the last, so the fourth request compacts.
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[
            *(_delegation(f"call-{n}", "writer", f"Part {n}", usage(1200 * n, 1)) for n in range(1, 6)),
            answer("Done"),
        ],
        writer=_big_answers(5),
        context_window=4000,
    )
    lifecycle = RecordingCompactionLifecycle()

    await team.stream(
        ctx=team.ctx(matrix_run_metadata=_SEEDED_METADATA),
        compaction_lifecycle=lifecycle,
    )

    assert summary_calls.inputs
    assert any(isinstance(event, CompactionOutcome) for event in lifecycle.events)
    assert "call-1" in team.recorder.folded_tool_call_ids
    record, _members = team.stored_runs()
    assert record.metadata is not None
    assert record.metadata["room_topic"] == "kept"
    # The folded usage is this run's own; the fold records seeded from the earlier run are gone.
    folded = record.metadata[COMPACTED_REQUESTS_METADATA_KEY]
    assert [request["metrics"]["input_tokens"] for request in folded] == [1200 * n for n in range(1, len(folded) + 1)]
    assert len(folded) >= 3


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_team_approval_resume_after_mid_turn_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    """A member's approval pauses the team after the leader compacted; the resumed leader request keeps that summary."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[
            *_delegations(4),
            _delegation("call-team", "checker", "Touch it"),
            answer("Touched and done"),
        ],
        writer=_big_answers(4),
        checker=[calls(tool_call("touch", "call-touch", label="dangerous")), answer("Touched.")],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
        context_window=4000,
    )
    _seed_team_history(team)
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    assert summary_calls.inputs
    assert ProbeToolkit.calls == []
    paused_request = team.models["default"].requests[-1]
    before = len(team.models["default"].requests)

    result = await team.resume(
        raised.value.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    assert ProbeToolkit.calls == ["dangerous"]
    resumed = team.models["default"].requests[before]
    [summary] = _summaries(resumed.messages)
    [paused_summary] = _summaries(paused_request.messages)
    assert summary.content == paused_summary.content
    stored = team.storage.load_session(SESSION_ID, "team")
    rows = summary_rows_of(team.storage, TEAM_SCOPE.key, SESSION_ID)
    assert stored is not None
    assert stored.summary is not None
    assert stored.summary.text == rows[-1].summary


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_the_team_s_call_budget_caps_a_request_before_compacting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    """The configured team budget refuses the fifth request before compaction could size it."""
    # The fourth response reports a request far over the window, so only the fifth request would compact.
    leader = [
        _delegation(f"call-{number}", "writer", f"Part {number}", usage(1_000_000, 1) if number == 4 else usage(10, 1))
        for number in range(1, 9)
    ]
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=leader,
        writer=[answer("ok", usage(10, 1)) for _ in range(8)],
        context_window=12_000,
        max_tool_calls_per_turn=2,
    )

    await team.stream()

    # The cap allows the limit plus two requests, so it refuses the fifth.
    assert len(team.models["default"].requests) == 4
    assert summary_calls.inputs == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_team_member_loop_compacts_run_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    """A member's own tool loop compacts within its run; the summary is the run's own, not replayed history."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[_delegation("call-team", "writer", "Dig it up"), answer("The writer dug.")],
        writer=[
            *(calls(tool_call("dig", f"dig-{number}")) for number in range(1, 6)),
            answer("Dug."),
        ],
        context_window=4000,
    )

    chunks = await team.stream()

    assert chunks[-1].content.endswith("The writer dug.")
    assert summary_calls.inputs
    compacted = _first_compacted(team.models["writer-model"])
    assert [message.from_history for message in _summaries(compacted.messages)] == [False]
    _record, [member] = team.stored_runs()
    assert member.status is RunStatus.completed


@dataclass
class _PerTaskLoop(ScriptedModel):
    """Script each delegated task's run on its own: ``dig`` for ``tool_rounds`` requests, then the task's answer.

    Concurrent runs of one member share this model and interleave their requests, so each request is answered from
    the script of the task it carries; ``tasks`` names that task for each of ``requests``. When ``task_b_waits_for``
    is given, Task B's run waits for it before answering its third request, so Task A's run outgrows its window
    first.
    """

    turns: list[Sequence[ScriptStep]] = field(default_factory=list)
    tool_rounds: int = 6
    tasks: list[str] = field(default_factory=list)
    task_b_waits_for: asyncio.Event | None = None

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Yield the next turn of the task ``request`` carries."""
        task = next(name for name in ("Task A", "Task B") if any(name in str(m.content) for m in request.messages))
        self.tasks.append(task)
        number = self.tasks.count(task)
        turn = (
            calls(tool_call("dig", f"call_{task[-1]}_{number}"))
            if number <= self.tool_rounds
            else answer(f"{task} done")
        )
        event = self.task_b_waits_for
        if event is not None and (task, number) == ("Task B", 3):

            async def wait() -> None:
                await asyncio.wait_for(event.wait(), timeout=5)

            turn = [Await(wait), *turn]
        # The base model takes the queued turn at once, before another run's request can queue its own.
        self.turns.append(turn)
        async for event in super().stream(request):
            yield event

    def compacted_tasks(self) -> list[str]:
        """Return the tasks whose runs sent a request that carries a compaction summary."""
        return sorted(
            {task for task, request in zip(self.tasks, self.requests, strict=True) if _summaries(request.messages)},
        )


def _two_writer_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TeamTurn, _PerTaskLoop]:
    """Return the squad whose leader hands the writer two tasks at once, each outgrowing the writer's window."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[
            calls(
                tool_call("delegate_task_to_member", "delegate-a", member_id="writer", task="Task A"),
                tool_call("delegate_task_to_member", "delegate-b", member_id="writer", task="Task B"),
            ),
            answer("Both done"),
        ],
        context_window=4000,
    )
    writer = _PerTaskLoop()
    team.models["writer-model"] = writer
    return team, writer


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_parallel_delegations_to_one_member_both_compact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    """Two concurrent runs of one member each compact their own loop."""
    team, writer = _two_writer_runs(tmp_path, monkeypatch)

    chunks = await team.stream()

    assert chunks[-1].content.endswith("Both done")
    assert summary_calls.inputs
    assert writer.compacted_tasks() == ["Task A", "Task B"]
    _record, members = team.stored_runs()
    assert sorted(str(member.content) for member in members) == ["Task A done", "Task B done"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_a_member_run_whose_compaction_failed_leaves_its_sibling_run_compacting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_calls: _SummaryCalls,
) -> None:
    """Each member run keeps its own compaction state: the run whose summary failed stops, its sibling still compacts."""
    team, writer = _two_writer_runs(tmp_path, monkeypatch)
    failed: list[str] = []
    writer.task_b_waits_for = asyncio.Event()

    async def fail_task_a(*, summary_input: str, **kwargs: Any) -> str:  # noqa: ANN401
        if "Task A" in summary_input:
            failed.append(summary_input)
            writer.task_b_waits_for.set()
            msg = "summary model unavailable"
            raise RuntimeError(msg)
        return await summary_calls(summary_input=summary_input, **kwargs)

    monkeypatch.setattr("mindroom.history.compaction.generate_compaction_summary", fail_task_a)

    chunks = await team.stream()

    assert chunks[-1].content.endswith("Both done")
    # Task A's run tried once and then stopped; Task B's run, which outgrew its window after that, still compacted.
    assert len(failed) == 1
    assert summary_calls.inputs
    assert writer.compacted_tasks() == ["Task B"]
    _record, members = team.stored_runs()
    assert sorted(str(member.content) for member in members) == ["Task A done", "Task B done"]
