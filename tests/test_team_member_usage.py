"""A team member's usage is kept when the member is stopped or fails."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest
from agno.agent import Agent
from agno.agent import _run as agent_run
from agno.db.base import BaseDb, SessionType
from agno.exceptions import ModelProviderError
from agno.metrics import MessageMetrics
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.run.cancel import acancel_run
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession
from agno.team import Team
from agno.team import _run as team_run
from agno.tools.function import Function

from mindroom import agno_compat_session_persistence
from mindroom.agent_storage import create_state_storage, save_runs
from mindroom.agno_compat_session_persistence import drain_cancelled_team_save
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.usage_stats import collect_admin_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

_HANG = "hang"


@dataclass
class _ScriptModel(Model):
    """Return scripted provider responses; a hang marker blocks until the request is stopped."""

    script: list[ModelResponse | Exception | str] = field(default_factory=list)
    hanging: asyncio.Event = field(default_factory=asyncio.Event)

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        raise NotImplementedError

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return await self._next()

    def invoke_stream(self, *_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        raise NotImplementedError

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        yield await self._next()

    def _parse_provider_response(self, response: ModelResponse, **_kwargs: object) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: ModelResponse, **_kwargs: object) -> ModelResponse:
        return response

    async def _next(self) -> ModelResponse:
        item = self.script.pop(0)
        if item == _HANG:
            self.hanging.set()
            await asyncio.Event().wait()
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, ModelResponse)
        return item


def _calling(name: str, arguments: str, input_tokens: int) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[
            {"id": f"call-{input_tokens}", "type": "function", "function": {"name": name, "arguments": arguments}},
        ],
        response_usage=MessageMetrics(input_tokens=input_tokens, output_tokens=0, total_tokens=input_tokens),
    )


def _answer(input_tokens: int) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        content="done",
        response_usage=MessageMetrics(input_tokens=input_tokens, output_tokens=0, total_tokens=input_tokens),
    )


def _delegation(*, collaborate: bool = False) -> ModelResponse:
    if collaborate:
        return _calling("delegate_task_to_members", '{"task": "Do the work"}', 10)
    return _calling("delegate_task_to_member", '{"member_id": "worker", "task": "Do the work"}', 10)


def _lookup() -> str:
    return "found"


@dataclass
class _Squad:
    team: Team
    leader: _ScriptModel
    member: _ScriptModel
    config: Config
    tmp_path: Path

    def model_tokens(self) -> dict[str, int]:
        paths = resolve_runtime_paths(
            config_path=self.tmp_path / "config.yaml",
            storage_path=self.tmp_path,
            process_env={},
        )
        report = collect_admin_usage(config=self.config, runtime_paths=paths)
        return {row.model: row.totals.total_tokens for row in report.model_breakdown}


def _squad(
    tmp_path: Path,
    *,
    leader: list[ModelResponse | Exception | str],
    member: list[ModelResponse | Exception | str],
    collaborate: bool = False,
) -> _Squad:
    config = Config(
        agents={"worker": AgentConfig(display_name="Worker")},
        teams={"squad": TeamConfig(display_name="Squad", role="Does work", agents=["worker"])},
    )
    storage = create_state_storage(
        "squad",
        tmp_path / "teams" / "squad",
        subdir="sessions",
        session_table="squad_sessions",
    )
    storage.upsert_session(TeamSession(session_id="session", team_id="squad"))
    leader_model = _ScriptModel(id="leader-model", provider="test-provider", script=leader)
    member_model = _ScriptModel(id="member-model", provider="test-provider", script=member)
    worker = Agent(
        id="worker",
        name="worker",
        model=member_model,
        telemetry=False,
        tools=[
            Function(name="lookup", entrypoint=_lookup),
            Function(name="publish", entrypoint=_lookup, requires_confirmation=True),
        ],
    )
    team = Team(
        id="squad",
        name="Squad",
        members=[worker],
        model=leader_model,
        db=storage,
        telemetry=False,
        delegate_to_all_members=collaborate,
    )
    return _Squad(team=team, leader=leader_model, member=member_model, config=config, tmp_path=tmp_path)


async def _run(squad: _Squad, *, stream: bool) -> None:
    if stream:
        async for _ in squad.team.arun(
            "Go",
            session_id="session",
            user_id="@alice:localhost",
            run_id="team-run",
            stream=True,
            stream_events=True,
            yield_run_output=True,
        ):
            pass
    else:
        await squad.team.arun("Go", session_id="session", user_id="@alice:localhost", run_id="team-run")


async def _finish_detached_saves() -> None:
    """Agno saves a stopped run on a detached task; wait for it like a later reader would."""
    while pending := [task for task in (*team_run._background_tasks, *agent_run._background_tasks) if not task.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "non-stream"])
@pytest.mark.parametrize("asks_agno_to_cancel", [False, True], ids=["task-cancel", "task-and-agno-cancel"])
@pytest.mark.parametrize("collaborate", [False, True], ids=["coordinate", "collaborate"])
async def test_stopped_team_member_keeps_its_usage(
    tmp_path: Path,
    stream: bool,
    asks_agno_to_cancel: bool,
    collaborate: bool,
) -> None:
    """A Stop while a member works keeps every request the member already made, and stops the member."""
    squad = _squad(
        tmp_path,
        leader=[_delegation(collaborate=collaborate), _answer(1)],
        member=[_calling("lookup", "{}", 100), _calling("lookup", "{}", 1000), _HANG],
        collaborate=collaborate,
    )
    reply = asyncio.create_task(_run(squad, stream=stream))
    await squad.member.hanging.wait()
    # MindRoom's Stop cancels the reply task and then asks Agno to cancel the run.
    reply.cancel()
    if asks_agno_to_cancel:
        await acancel_run("team-run")
    with pytest.raises(asyncio.CancelledError):
        await reply
    await _finish_detached_saves()

    assert squad.model_tokens() == {"leader-model": 10, "member-model": 1100}
    assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "non-stream"])
async def test_failed_team_member_keeps_its_usage_once(tmp_path: Path, stream: bool) -> None:
    """A member whose provider fails keeps the requests it made, and the team still answers."""
    squad = _squad(
        tmp_path,
        leader=[_delegation(), _answer(1)],
        member=[
            _calling("lookup", "{}", 100),
            ModelProviderError("overloaded", status_code=529, model_name="member-model", model_id="member-model"),
        ],
    )

    await _run(squad, stream=stream)

    assert squad.model_tokens() == {"leader-model": 11, "member-model": 100}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "non-stream"])
async def test_finished_team_member_counts_once(tmp_path: Path, stream: bool) -> None:
    """A member that finishes is counted exactly once."""
    squad = _squad(
        tmp_path,
        leader=[_delegation(), _answer(1)],
        member=[_calling("lookup", "{}", 100), _answer(1000)],
    )

    await _run(squad, stream=stream)

    assert squad.model_tokens() == {"leader-model": 11, "member-model": 1100}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "non-stream"])
@pytest.mark.parametrize("stored_run", [False, True], ids=["by-run-id", "stored-run"])
async def test_member_stopped_after_approval_keeps_its_usage(tmp_path: Path, stream: bool, stored_run: bool) -> None:
    """A member resumed after an approval keeps the requests it makes before a Stop, as well as those before it paused."""
    squad = _squad(
        tmp_path,
        leader=[_delegation(), _answer(1)],
        member=[_calling("publish", "{}", 100), _calling("lookup", "{}", 1000), _HANG],
    )
    paused = await squad.team.arun("Go", session_id="session", user_id="@alice:localhost", run_id="team-run")
    assert paused.is_paused
    # MindRoom resumes the paused run it reads back from storage; Agno can also look it up by id.
    database = cast("BaseDb", squad.team.db)
    session = database.get_session("session", session_type=SessionType.TEAM)
    assert isinstance(session, TeamSession)
    stored = session.get_run("team-run")
    assert isinstance(stored, TeamRunOutput)
    requirements = (stored if stored_run else paused).requirements or []
    for requirement in requirements:
        requirement.confirm()
    resume_from = {"run_response": stored} if stored_run else {"run_id": "team-run"}

    async def resume() -> None:
        if stream:
            async for _ in squad.team.acontinue_run(
                **resume_from,
                requirements=requirements,
                session_id="session",
                user_id="@alice:localhost",
                stream=True,
            ):
                pass
        else:
            await squad.team.acontinue_run(
                **resume_from,
                requirements=requirements,
                session_id="session",
                user_id="@alice:localhost",
            )

    reply = asyncio.create_task(resume())
    await squad.member.hanging.wait()
    reply.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reply
    await _finish_detached_saves()

    assert squad.model_tokens() == {"leader-model": 10, "member-model": 1100}


@dataclass
class _SlowToStopModel(_ScriptModel):
    """Take a while to unwind when stopped, like closing a provider connection or a subprocess."""

    async def _next(self) -> ModelResponse:
        try:
            return await super()._next()
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.sleep(0.05))
            raise


@pytest.mark.asyncio
async def test_interrupted_turn_written_after_a_stop_is_not_overwritten(tmp_path: Path) -> None:
    """MindRoom's own record of a stopped turn lands after Agno's save of it, even while members take time to stop."""
    squad = _squad(
        tmp_path,
        leader=[_delegation(), _answer(1)],
        member=[_calling("lookup", "{}", 100), _calling("lookup", "{}", 1000), _HANG],
    )
    member = squad.team.members[0]
    assert isinstance(member, Agent)
    member.model = squad.member = _SlowToStopModel(
        id="member-model",
        provider="test-provider",
        script=squad.member.script,
    )
    database = cast("BaseDb", squad.team.db)
    session = database.get_session("session", session_type=SessionType.TEAM)
    assert isinstance(session, TeamSession)
    # Create the runs table up front, as an earlier turn would have.
    save_runs(database, session, [TeamRunOutput(run_id="earlier", team_id="squad", session_id="session", content="Hi")])
    reply = asyncio.create_task(_run(squad, stream=False))
    await squad.member.hanging.wait()
    reply.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reply

    await drain_cancelled_team_save("team-run")
    interrupted = TeamRunOutput(
        run_id="team-run",
        team_id="squad",
        session_id="session",
        content="Partial answer",
        status=RunStatus.completed,
        messages=[Message(role="user", content="Go"), Message(role="assistant", content="Partial answer")],
    )
    await asyncio.to_thread(save_runs, database, session, [interrupted])
    await _finish_detached_saves()

    stored = database.get_session("session", session_type=SessionType.TEAM)
    assert isinstance(stored, TeamSession)
    run = stored.get_run("team-run")
    assert run is not None
    assert run.status == RunStatus.completed
    assert squad.model_tokens()["member-model"] == 1100


@pytest.mark.asyncio
async def test_interrupted_turn_waits_only_for_its_own_team_save(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping one team reply never waits for another conversation's stopped team to finish saving."""
    agno_compat_session_persistence.install_patch()
    released = asyncio.Event()

    def schedule_slow_save(*_args: object) -> None:
        team_run._background_tasks.add(asyncio.create_task(released.wait()))

    monkeypatch.setattr(agno_compat_session_persistence, "_ORIGINAL_CANCELLED_TEAM_RUN_PERSIST", schedule_slow_save)
    team = Team(id="squad", members=[], telemetry=False)
    team_run._persist_cancelled_team_run_in_background(
        team,
        TeamRunOutput(run_id="other-run", team_id="squad", session_id="other"),
        TeamSession(session_id="other", team_id="squad"),
    )
    try:
        async with asyncio.timeout(5):
            await drain_cancelled_team_save("team-run")
        assert "other-run" in agno_compat_session_persistence._CANCELLED_TEAM_SAVES
    finally:
        released.set()
        await _finish_detached_saves()
    assert "other-run" not in agno_compat_session_persistence._CANCELLED_TEAM_SAVES
