"""Behavioral tests for asynchronous Agno session persistence."""

from __future__ import annotations

import asyncio
import threading
import time
from importlib import import_module
from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.team import Team

from mindroom import agent_storage
from mindroom.agent_storage import create_state_storage

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
    created_at = int(time.time())
    messages = [
        Message(role="system", content="prompt"),
        Message(role="user", content="question"),
    ]
    if surface == "agent":
        owner = Agent(db=storage, telemetry=False)
        session = AgentSession(
            session_id=session_id,
            session_data=session_data,
            created_at=created_at,
            runs=[RunOutput(run_id="run", session_id=session_id, messages=messages)],
        )
        return owner, session

    owner = Team(db=storage, members=[], telemetry=False)
    session = TeamSession(
        session_id=session_id,
        session_data=session_data,
        created_at=created_at,
        runs=[TeamRunOutput(run_id="run", session_id=session_id, messages=messages)],
    )
    return owner, session


def test_supported_agno_session_sources_are_patched() -> None:
    """A dependency upgrade must make the guarded compatibility boundary visible to CI."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")

    assert persistence_patch.apply_patch() is True
    assert persistence_patch._is_applied() is True


@pytest.mark.parametrize("surface", ["agent", "team"])
@pytest.mark.asyncio
async def test_async_save_runs_application_storage_work_off_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
) -> None:
    """Prompt cleanup, serialization, and SQLite persistence must all leave the event loop."""
    storage = _storage(tmp_path, surface)
    owner, session = _owner_and_session(surface, storage, f"{surface}-session")
    original_sanitizer = agent_storage._session_without_prompt_messages
    event_loop_thread = threading.get_ident()
    sanitizer_threads: list[int] = []
    loop_progressed = asyncio.Event()

    def delayed_sanitizer(
        session_to_save: AgentSession | TeamSession,
        prompt_roles: frozenset[str],
    ) -> AgentSession | TeamSession:
        sanitizer_threads.append(threading.get_ident())
        time.sleep(0.1)
        return original_sanitizer(session_to_save, prompt_roles)

    async def mark_loop_progress() -> None:
        await asyncio.sleep(0.01)
        loop_progressed.set()

    monkeypatch.setattr(agent_storage, "_session_without_prompt_messages", delayed_sanitizer)
    progress_task = asyncio.create_task(mark_loop_progress())
    try:
        await owner.asave_session(session)
        assert loop_progressed.is_set()
        assert sanitizer_threads
        assert sanitizer_threads[0] != event_loop_thread
    finally:
        await progress_task
        storage.close()


@pytest.mark.asyncio
async def test_agent_and_team_saves_are_ordered_when_they_share_a_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later save on one database must not enter persistence before an earlier save completes."""
    storage = _storage(tmp_path)
    agent, agent_session = _owner_and_session("agent", storage, "first")
    team, team_session = _owner_and_session("team", storage, "second")
    first_started = threading.Event()
    release_first = threading.Event()
    started: list[str] = []
    original_upsert = storage.upsert_session

    def ordered_probe(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        started.append(session.session_id)
        if session.session_id == "first":
            first_started.set()
            release_first.wait(timeout=0.25)
        return original_upsert(session, deserialize=deserialize)

    async def wait_until_started() -> None:
        for _ in range(100):
            if first_started.is_set():
                return
            await asyncio.sleep(0.005)
        pytest.fail("first save did not start")

    monkeypatch.setattr(storage, "upsert_session", ordered_probe)
    first_save = asyncio.create_task(agent.asave_session(agent_session))
    second_save: asyncio.Task[None] | None = None
    try:
        await wait_until_started()
        second_save = asyncio.create_task(team.asave_session(team_session))
        await asyncio.sleep(0.02)
        assert started == ["first"]
        assert not second_save.done()
        release_first.set()
        await asyncio.gather(first_save, second_save)
    finally:
        release_first.set()
        tasks = [first_save, *([second_save] if second_save is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
        storage.close()

    assert started == ["first", "second"]
