"""Session accounting counts each run contribution once, including resumptions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import pytest
from agno.agent import Agent
from agno.agent._run import acheckpoint_run, checkpoint_run, persist_run_in_session
from agno.db.base import SessionType
from agno.metrics import MessageMetrics, ModelMetrics, RunMetrics
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.team import Team
from agno.team._run import _cleanup_and_store
from agno.tools.function import Function

from mindroom.agent_storage import create_state_storage
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.usage_stats import collect_admin_usage
from tests.history_helpers import RecordingModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path


def _metrics(scale: int) -> RunMetrics:
    counters = {
        "input_tokens": 7 * scale,
        "output_tokens": 3 * scale,
        "total_tokens": 10 * scale,
        "cache_read_tokens": 2 * scale,
        "cache_write_tokens": 4 * scale,
        "reasoning_tokens": scale,
        "audio_input_tokens": 5 * scale,
        "audio_output_tokens": 6 * scale,
        "audio_total_tokens": 11 * scale,
        "cost": 0.25 * scale,
    }
    return RunMetrics(
        **counters,
        additional_metrics={"requests": scale, "label": "latest"},
        details={
            "model": [
                ModelMetrics(
                    id="test-model",
                    provider="test-provider",
                    provider_metrics={"requests": scale, "label": "latest"},
                    **counters,
                ),
            ],
        },
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_checkpoints_count_only_new_metrics_despite_shared_run_objects(
    tmp_path: Path,
    asynchronous: bool,
) -> None:
    """Bare sessions and repeated shallow checkpoints must retain each usage contribution once."""
    storage = create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")
    actor = Agent(id="code", db=storage, checkpoint="tool-batch", telemetry=False)
    session = AgentSession(session_id="session", agent_id="code")
    run = RunOutput(run_id="run", agent_id="code", metrics=_metrics(1))
    # Session summaries can upsert a run before accounting it.
    session.upsert_run(run)
    session.upsert_run(run)
    try:
        for scale in (1, 1, 2, 2):
            # Keep the same metrics object: Agno's checkpoint storage copy is shallow.
            run.metrics.__dict__.update(_metrics(scale).__dict__)
            if asynchronous:
                await acheckpoint_run(actor, run, session)
            else:
                checkpoint_run(actor, run, session)
            saved = storage.get_session("session", session_type=SessionType.AGENT)
            assert isinstance(saved, AgentSession)
            actual = saved.session_data["session_metrics"]
            expected = _metrics(scale).to_dict()
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "audio_input_tokens",
                "audio_output_tokens",
                "audio_total_tokens",
                "cost",
                "additional_metrics",
                "details",
            ):
                assert actual[key] == expected[key]
            assert session.session_data["session_metrics"] == actual
    finally:
        storage.close()


def test_resuming_preserves_usage_no_longer_present_in_conversation(tmp_path: Path) -> None:
    """A delta fix must not replace historical cumulative usage with retained-run sums."""
    storage = create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")
    seeded = AgentSession(
        session_id="session",
        agent_id="code",
        session_data={"session_metrics": _metrics(10).to_dict()},
    )
    storage.upsert_session(seeded)
    storage.upsert_run(RunOutput(run_id="old", agent_id="code", metrics=_metrics(1)), "session")
    storage.close()

    storage = create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")
    actor = Agent(id="code", db=storage, telemetry=False)
    try:
        session = storage.get_session("session", session_type=SessionType.AGENT)
        assert isinstance(session, AgentSession)
        run = deepcopy(session.runs[0])
        run.metrics = _metrics(2)
        persist_run_in_session(actor, run, session)
        assert session.session_data["session_metrics"]["total_tokens"] == 110

        storage.delete_runs(["old"])
        session = storage.get_session("session", session_type=SessionType.AGENT)
        assert isinstance(session, AgentSession)
        persist_run_in_session(actor, RunOutput(run_id="new", agent_id="code", metrics=_metrics(3)), session)
        saved = storage.get_session("session", session_type=SessionType.AGENT)
        assert isinstance(saved, AgentSession)
        assert saved.session_data["session_metrics"]["total_tokens"] == 140
        assert saved.session_data["session_metrics"]["details"]["model"][0]["total_tokens"] == 140
    finally:
        storage.close()


@pytest.mark.parametrize("reopen", [False, True])
def test_team_resaves_count_member_and_nested_team_deltas_once(tmp_path: Path, reopen: bool) -> None:
    """Team history can store flat member rows as well as nested response objects."""
    storage = create_state_storage("squad", tmp_path, subdir="sessions", session_table="squad_sessions")
    actor = Team(id="squad", members=[], db=storage, telemetry=False, store_member_responses=True)
    session = TeamSession(session_id="session", team_id="squad")
    child = RunOutput(run_id="child", agent_id="child", metrics=_metrics(4))
    nested = TeamRunOutput(run_id="nested", team_id="nested", metrics=_metrics(3), member_responses=[child])
    member = RunOutput(run_id="member", agent_id="member", parent_run_id="leader", metrics=_metrics(2))
    run = TeamRunOutput(run_id="leader", team_id="squad", metrics=_metrics(1), member_responses=[member, nested])
    session.upsert_run(member)
    try:
        _cleanup_and_store(actor, run, session)
        assert session.session_data["session_metrics"]["total_tokens"] == 100
        if reopen:
            storage.close()
            storage = create_state_storage("squad", tmp_path, subdir="sessions", session_table="squad_sessions")
            actor = Team(id="squad", members=[], db=storage, telemetry=False, store_member_responses=True)
            session = storage.get_session("session", session_type=SessionType.TEAM)
            assert isinstance(session, TeamSession)
        run.metrics = _metrics(2)
        member.metrics = _metrics(3)
        _cleanup_and_store(actor, run, session)
        saved = storage.get_session("session", session_type=SessionType.TEAM)
        assert isinstance(saved, TeamSession)
        assert saved.session_data["session_metrics"]["total_tokens"] == 120
        assert saved.session_data["session_metrics"]["cost"] == 3
        assert saved.session_data["session_metrics"]["details"]["model"][0]["total_tokens"] == 120
    finally:
        storage.close()


@dataclass
class _ApprovalModel(RecordingModel):
    responses: list[ModelResponse] = field(default_factory=list)

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return self.responses.pop(0)

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return self.invoke()

    def invoke_stream(self, *_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        yield self.invoke()

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        yield self.invoke()


async def _execute_approval_step(
    agent: Agent | Team,
    paused: RunOutput | TeamRunOutput | None,
    mode: str,
) -> RunOutput | TeamRunOutput:
    kwargs: dict[str, Any] = {
        "session_id": "session",
        "user_id": "@alice:example.test",
        "metadata": {"requester_id": "@alice:example.test"},
    }
    if mode.endswith("stream"):
        kwargs.update(stream=True, stream_events=True, yield_run_output=True)
    if paused is not None:
        for requirement in paused.requirements or ():
            if requirement.needs_confirmation:
                requirement.confirm()
        kwargs.update(run_id=paused.run_id, requirements=paused.requirements)
        response = agent.acontinue_run(**kwargs) if mode.startswith("async") else agent.continue_run(**kwargs)
    else:
        response = (
            agent.arun("Run the tool", **kwargs) if mode.startswith("async") else agent.run("Run the tool", **kwargs)
        )
    if mode == "async_stream":
        outputs = [event async for event in response if isinstance(event, (RunOutput, TeamRunOutput))]
        return outputs[-1]
    if mode == "sync_stream":
        return [event for event in response if isinstance(event, (RunOutput, TeamRunOutput))][-1]
    return await response if mode == "async" else response


@pytest.mark.parametrize("mode", ["sync", "sync_stream", "async", "async_stream"])
@pytest.mark.parametrize("pauses", [0, 1, 3])
@pytest.mark.asyncio
async def test_approval_usage_reconciles_with_export_after_each_resume(
    tmp_path: Path,
    mode: Literal["sync", "sync_stream", "async", "async_stream"],
    pauses: int,
) -> None:
    """Real lifecycle and export totals must agree after pauses, process reconstruction and completion."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    config = Config(agents={"code": AgentConfig(display_name="Code")})
    root = tmp_path / "agents" / "code"
    storage = create_state_storage("code", root, subdir="sessions", session_table="code_sessions")

    def approved_tool() -> str:
        return "Done"

    responses = [
        ModelResponse(
            role="assistant",
            tool_calls=[
                {"id": f"call-{index}", "type": "function", "function": {"name": "approved_tool", "arguments": "{}"}},
            ],
            response_usage=MessageMetrics(input_tokens=7, output_tokens=3, total_tokens=10, cache_read_tokens=2),
        )
        for index in range(pauses)
    ]
    responses.append(
        ModelResponse(
            role="assistant",
            content="Complete",
            response_usage=MessageMetrics(input_tokens=14, output_tokens=6, total_tokens=20, cache_read_tokens=4),
        ),
    )
    model = _ApprovalModel(id="test-model", provider="test-provider", responses=responses)

    def actor() -> Agent:
        return Agent(
            id="code",
            db=storage,
            model=model,
            telemetry=False,
            cache_session=True,
            tools=[Function(name="approved_tool", entrypoint=approved_tool, requires_confirmation=True)],
        )

    try:
        agent = actor()
        response = await _execute_approval_step(agent, None, mode)
        for completed_pauses in range(pauses + 1):
            expected_tokens = 10 * (completed_pauses + 1) if completed_pauses < pauses else 10 * pauses + 20
            report = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True, include_requests=True)
            assert report.totals.total_tokens == expected_tokens
            assert sum(row.totals.total_tokens for row in report.model_breakdown) == expected_tokens
            assert sum(row.totals.total_tokens for row in report.cumulative_model_breakdown) == expected_tokens
            assert sum(row.totals.total_tokens for row in report.user_breakdown) == expected_tokens
            assert sum(row.totals.total_tokens for row in report.daily_breakdown) == expected_tokens
            assert len(report.request_breakdown) == completed_pauses + 1
            assert sum(row.totals.total_tokens for row in report.request_breakdown) == expected_tokens
            assert report.request_coverage is not None
            assert report.request_coverage.unavailable_sources == 0
            if completed_pauses == pauses:
                assert response.status == RunStatus.completed
                break
            assert response.status == RunStatus.paused
            # One resume reconstructs storage and actor; others exercise the cached session.
            if completed_pauses == 0:
                storage.close()
                storage = create_state_storage("code", root, subdir="sessions", session_table="code_sessions")
                agent = actor()
            response = await _execute_approval_step(agent, response, mode)
    finally:
        storage.close()


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.asyncio
async def test_team_approval_counts_leader_and_member_usage_once(tmp_path: Path, mode: str, reopen: bool) -> None:
    """Default team storage must account resumed members, including after process reconstruction."""
    storage = create_state_storage("squad", tmp_path, subdir="sessions", session_table="squad_sessions")
    storage.upsert_session(TeamSession(session_id="session", team_id="squad"))
    leader_model = _ApprovalModel(
        id="leader-model",
        provider="test-provider",
        responses=[
            ModelResponse(
                role="assistant",
                tool_calls=[
                    {
                        "id": "delegate",
                        "type": "function",
                        "function": {
                            "name": "delegate_task_to_member",
                            "arguments": '{"member_id":"worker","task":"Do work"}',
                        },
                    },
                ],
                response_usage=MessageMetrics(total_tokens=10),
            ),
            ModelResponse(role="assistant", content="Complete", response_usage=MessageMetrics(total_tokens=30)),
        ],
    )
    member_model = _ApprovalModel(
        id="member-model",
        provider="test-provider",
        responses=[
            ModelResponse(
                role="assistant",
                tool_calls=[
                    {"id": "call", "type": "function", "function": {"name": "approved_tool", "arguments": "{}"}},
                ],
                response_usage=MessageMetrics(total_tokens=20),
            ),
            ModelResponse(role="assistant", content="Complete", response_usage=MessageMetrics(total_tokens=40)),
        ],
    )

    def approved_tool() -> str:
        return "Done"

    def actor() -> Team:
        member = Agent(
            id="worker",
            name="worker",
            db=storage,
            model=member_model,
            telemetry=False,
            tools=[Function(name="approved_tool", entrypoint=approved_tool, requires_confirmation=True)],
        )
        return Team(id="squad", members=[member], db=storage, model=leader_model, telemetry=False, cache_session=True)

    try:
        team = actor()
        response = await _execute_approval_step(team, None, mode)
        assert response.status == RunStatus.paused
        saved = storage.get_session("session", session_type=SessionType.TEAM)
        assert isinstance(saved, TeamSession)
        assert saved.session_data["session_metrics"]["total_tokens"] == 30
        if reopen:
            storage.close()
            storage = create_state_storage("squad", tmp_path, subdir="sessions", session_table="squad_sessions")
            team = actor()
            saved = storage.get_session("session", session_type=SessionType.TEAM)
            assert isinstance(saved, TeamSession)
            response = saved.get_run(response.run_id)
            assert isinstance(response, TeamRunOutput)
        response = await _execute_approval_step(team, response, mode)
        assert response.status == RunStatus.completed
        saved = storage.get_session("session", session_type=SessionType.TEAM)
        assert isinstance(saved, TeamSession)
        metrics = saved.session_data["session_metrics"]
        assert metrics["total_tokens"] == 100
        assert {row["id"]: row["total_tokens"] for row in metrics["details"]["model"]} == {
            "leader-model": 40,
            "member-model": 60,
        }
    finally:
        storage.close()
