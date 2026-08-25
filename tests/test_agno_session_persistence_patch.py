"""Behavioral tests for the guarded Agno session persistence boundary."""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from importlib import import_module
from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.agent import _run as agent_run
from agno.db.sqlite.async_sqlite import AsyncSqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.session.workflow import WorkflowSession
from agno.team import Team
from agno.team import _run as team_run
from agno.workflow import Workflow

from mindroom.agent_storage import create_state_storage, get_agent_session, get_team_session

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb

type _Surface = Literal["agent", "team"]


def _storage(tmp_path: Path, name: str = "sessions") -> BaseDb:
    return create_state_storage(
        name,
        tmp_path,
        subdir=name,
        session_table=f"{name}_sessions",
        prompt_roles=frozenset({"system", "developer"}),
    )


def _owner_and_session(
    surface: _Surface,
    storage: BaseDb,
    session_id: str,
) -> tuple[Agent | Team, AgentSession | TeamSession]:
    session_data = {"session_state": {"current_run_id": "run"}}
    messages = [Message(role="user", content=session_id)]
    if surface == "agent":
        return Agent(db=storage, telemetry=False), AgentSession(
            session_id=session_id,
            session_data=session_data,
            created_at=int(time.time()),
            runs=[RunOutput(run_id="run", session_id=session_id, messages=messages)],
        )
    return Team(db=storage, members=[], telemetry=False), TeamSession(
        session_id=session_id,
        session_data=session_data,
        created_at=int(time.time()),
        runs=[TeamRunOutput(run_id="run", session_id=session_id, messages=messages)],
    )


def test_supported_agno_session_sources_are_patched() -> None:
    """Pinned source compatibility must remain visible to CI."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")

    assert persistence_patch._apply_patch() is True
    assert persistence_patch._is_applied() is True


def test_installation_fails_closed_when_a_pinned_source_drifts() -> None:
    """The application must not silently restore event-loop blocking after drift."""
    code = """
from importlib import import_module

patch = import_module("mindroom.agno_session_persistence_patch")
patch._EXPECTED_SOURCE_HASHES["agent_asave_session"] = "drift"
try:
    import_module("mindroom.agent_storage")
except RuntimeError as error:
    assert "session persistence" in str(error).lower()
else:
    raise AssertionError("source drift did not fail closed")
"""
    result = subprocess.run(
        ["uv", "run", "python", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_sync_saves_are_ordered_across_event_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One shared DB must preserve save invocation order across event loops."""
    storage = _storage(tmp_path)
    agent, agent_session = _owner_and_session("agent", storage, "first")
    team, team_session = _owner_and_session("team", storage, "second")
    first_started = threading.Event()
    release_first = threading.Event()
    started: list[str] = []
    started_guard = threading.Lock()
    errors: list[BaseException] = []
    original_upsert = storage.upsert_session

    def ordered_probe(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        with started_guard:
            started.append(session.session_id)
        if session.session_id == "first":
            first_started.set()
            assert release_first.wait(timeout=5)
        return original_upsert(session, deserialize=deserialize)

    def run_save(owner: Agent | Team, session: AgentSession | TeamSession) -> None:
        try:
            asyncio.run(owner.asave_session(session))
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    monkeypatch.setattr(storage, "upsert_session", ordered_probe)
    first = threading.Thread(target=run_save, args=(agent, agent_session))
    second = threading.Thread(target=run_save, args=(team, team_session))
    first.start()
    try:
        assert first_started.wait(timeout=5)
        second.start()
        time.sleep(0.1)
        assert started == ["first"]
    finally:
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)
        storage.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert started == ["first", "second"]


@pytest.mark.asyncio
async def test_cancellation_drains_write_before_later_save_and_close(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation must not detach a worker or release its ordering ticket."""
    storage = _storage(tmp_path)
    first_owner, first_session = _owner_and_session("agent", storage, "first")
    second_owner, second_session = _owner_and_session("team", storage, "second")
    first_started = threading.Event()
    release_first = threading.Event()
    started: list[str] = []
    active_writes = 0
    original_upsert = storage.upsert_session
    original_close = storage.close

    def blocking_upsert(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        nonlocal active_writes
        started.append(session.session_id)
        active_writes += 1
        try:
            if session.session_id == "first":
                first_started.set()
                assert release_first.wait(timeout=5)
            return original_upsert(session, deserialize=deserialize)
        finally:
            active_writes -= 1

    def checked_close() -> None:
        assert active_writes == 0
        original_close()

    def eventual_release() -> None:
        if first_started.wait(timeout=5):
            time.sleep(0.4)
            release_first.set()

    monkeypatch.setattr(storage, "upsert_session", blocking_upsert)
    monkeypatch.setattr(storage, "close", checked_close)
    release_thread = threading.Thread(target=eventual_release)
    release_thread.start()
    first = asyncio.create_task(first_owner.asave_session(first_session))
    second: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(first_started.wait, 5)
        first.cancel()
        second = asyncio.create_task(second_owner.asave_session(second_session))
        await asyncio.sleep(0.05)
        assert not first.done()
        assert started == ["first"]
        release_first.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        storage.close()
    finally:
        release_first.set()
        release_thread.join(timeout=1)
        tasks = [first, *([second] if second is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
        if storage.db_engine is not None:
            original_close()

    assert started == ["first", "second"]


@pytest.mark.asyncio
async def test_worker_error_propagates_and_does_not_strand_later_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected worker errors must surface and advance the database queue."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
    storage = _storage(tmp_path)
    owner, first_session = _owner_and_session("agent", storage, "first")
    _, second_session = _owner_and_session("agent", storage, "second")
    original_upsert = persistence_patch._ORIGINAL_AGENT_UPSERT_SESSION
    calls = 0

    def fail_once(agent: Agent, session: AgentSession) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            error_message = "write failed"
            raise RuntimeError(error_message)
        return original_upsert(agent, session)

    monkeypatch.setattr(persistence_patch, "_ORIGINAL_AGENT_UPSERT_SESSION", fail_once)
    try:
        with pytest.raises(RuntimeError, match="write failed"):
            await owner.asave_session(first_session)
        await owner.asave_session(second_session)
        assert get_agent_session(storage, "second") is not None
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_worker_error_wins_over_cancellation_after_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled waiter must still observe a submitted worker failure."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
    storage = _storage(tmp_path, "worker-error")
    owner, session = _owner_and_session("agent", storage, "worker-error")
    write_started = threading.Event()
    release_write = threading.Event()

    def failing_upsert(_agent: Agent, _session: AgentSession) -> None:
        write_started.set()
        assert release_write.wait(timeout=5)
        error_message = "write failed after cancellation"
        raise RuntimeError(error_message)

    monkeypatch.setattr(persistence_patch, "_ORIGINAL_AGENT_UPSERT_SESSION", failing_upsert)
    save = asyncio.create_task(owner.asave_session(session))
    try:
        assert await asyncio.to_thread(write_started.wait, 5)
        save.cancel()
        await asyncio.sleep(0)
        assert not save.done()
        release_write.set()
        with pytest.raises(RuntimeError, match="write failed after cancellation"):
            await save
    finally:
        release_write.set()
        await asyncio.gather(save, return_exceptions=True)
        storage.close()


@pytest.mark.asyncio
async def test_async_database_delegates_to_upstream_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A native async database must not enter the sync snapshot queue."""
    database = AsyncSqliteDb(db_file=":memory:")
    owner = Agent(db=database, telemetry=False)
    session = AgentSession(
        session_id="async",
        session_data={"session_state": {"current_run_id": "run"}},
    )
    calls: list[AgentSession] = []

    async def async_upsert(session: AgentSession) -> AgentSession:
        calls.append(session)
        return session

    monkeypatch.setattr(database, "upsert_session", async_upsert)
    await owner.asave_session(session)

    assert calls == [session]
    assert session.session_data == {"session_state": {}}


@pytest.mark.asyncio
async def test_team_save_preserves_live_member_response_scrubbing(tmp_path: Path) -> None:
    """Snapshotting must retain Agno's mutation of the live TeamSession."""
    storage = _storage(tmp_path, "team-scrub")
    team = Team(db=storage, members=[], store_member_responses=False, telemetry=False)
    member_response = RunOutput(run_id="member", agent_id="member", content="member")
    run = TeamRunOutput(run_id="run", team_id="team", member_responses=[member_response])
    session = TeamSession(
        session_id="team-scrub",
        team_id="team",
        session_data={"session_state": {"current_run_id": "run"}},
        runs=[run],
        created_at=int(time.time()),
    )
    try:
        await team.asave_session(session)
        persisted = get_team_session(storage, session.session_id)
    finally:
        storage.close()

    assert run.member_responses == []
    assert persisted is not None
    assert persisted.runs is not None
    assert isinstance(persisted.runs[0], TeamRunOutput)
    assert persisted.runs[0].member_responses == []


@pytest.mark.parametrize("surface", ["agent", "team"])
@pytest.mark.asyncio
async def test_agno_background_tasks_keep_upstream_synchronous_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
) -> None:
    """Library-owned detached tasks must not outlive their synchronous DB handle."""
    storage = _storage(tmp_path, f"{surface}-background")
    owner, session = _owner_and_session(surface, storage, "background")
    event_loop_thread = threading.get_ident()
    persistence_threads: list[int] = []
    original_upsert = storage.upsert_session
    background_tasks = agent_run._background_tasks if surface == "agent" else team_run._background_tasks

    def record_thread(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        persistence_threads.append(threading.get_ident())
        return original_upsert(session, deserialize=deserialize)

    monkeypatch.setattr(storage, "upsert_session", record_thread)
    task = asyncio.current_task()
    assert task is not None
    background_tasks.add(task)
    try:
        await owner.asave_session(session)
    finally:
        background_tasks.discard(task)
        storage.close()

    assert persistence_threads == [event_loop_thread]


@pytest.mark.asyncio
async def test_workflow_sync_save_remains_upstream_and_responsive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow already owns an upstream thread boundary and stays unpatched."""
    storage = _storage(tmp_path, "workflow")
    workflow = Workflow(db=storage, telemetry=False)
    session = WorkflowSession(
        session_id="workflow",
        session_data={"session_state": {"current_run_id": "run"}},
    )
    loop_progressed = asyncio.Event()
    original_upsert = storage.upsert_session

    def delayed_upsert(session: WorkflowSession, deserialize: bool | None = True) -> object:
        time.sleep(0.1)
        return original_upsert(session, deserialize=deserialize)

    async def mark_progress() -> None:
        await asyncio.sleep(0.01)
        loop_progressed.set()

    monkeypatch.setattr(storage, "upsert_session", delayed_upsert)
    progress = asyncio.create_task(mark_progress())
    try:
        await workflow.asave_session(session)
        assert loop_progressed.is_set()
        assert Workflow.asave_session.__module__.startswith("agno.")
    finally:
        await progress
        storage.close()
