"""Regression tests for synchronous Agno session persistence atomicity."""

from __future__ import annotations

import asyncio
import threading
import time
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
from mindroom.agent_storage import create_state_storage, get_agent_session, get_team_session

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb

type _Surface = Literal["agent", "team"]


def _storage(tmp_path: Path, name: str) -> BaseDb:
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
    messages = [
        Message(role="system", content="prompt"),
        Message(role="user", content="question"),
    ]
    if surface == "agent":
        return Agent(db=storage, telemetry=False), AgentSession(
            session_id=session_id,
            user_id="before",
            session_data={"session_state": {"current_run_id": "run"}},
            created_at=int(time.time()),
            runs=[RunOutput(run_id="run", session_id=session_id, messages=messages)],
        )
    return Team(db=storage, members=[], telemetry=False), TeamSession(
        session_id=session_id,
        user_id="before",
        session_data={"session_state": {"current_run_id": "run"}},
        created_at=int(time.time()),
        runs=[TeamRunOutput(run_id="run", session_id=session_id, messages=messages)],
    )


@pytest.mark.parametrize("surface", ["agent", "team"])
@pytest.mark.asyncio
async def test_live_session_cannot_mutate_while_sync_save_is_copying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
) -> None:
    """Do not move a live Session across threads without an exclusive owner."""
    storage = _storage(tmp_path, f"{surface}-atomic")
    owner, session = _owner_and_session(surface, storage, f"{surface}-session")
    sanitizer_started = threading.Event()
    loop_observed_sanitizer = asyncio.Event()
    release_sanitizer = threading.Event()
    event_loop = asyncio.get_running_loop()
    original_sanitizer = agent_storage._session_without_prompt_messages

    def blocked_sanitizer(
        session_to_save: AgentSession | TeamSession,
        prompt_roles: frozenset[str],
    ) -> AgentSession | TeamSession:
        sanitizer_started.set()
        event_loop.call_soon_threadsafe(loop_observed_sanitizer.set)
        assert release_sanitizer.wait(timeout=1)
        return original_sanitizer(session_to_save, prompt_roles)

    monkeypatch.setattr(agent_storage, "_session_without_prompt_messages", blocked_sanitizer)

    def release_after_mutation_window() -> None:
        if sanitizer_started.wait(timeout=5):
            time.sleep(0.2)
            release_sanitizer.set()

    release_thread = threading.Thread(target=release_after_mutation_window)
    release_thread.start()

    async def mutate_after_sanitizer_starts() -> None:
        await loop_observed_sanitizer.wait()
        session.user_id = "after"

    save = asyncio.create_task(owner.asave_session(session))
    mutation = asyncio.create_task(mutate_after_sanitizer_starts())
    try:
        await asyncio.gather(save, mutation)
        persisted = (
            get_agent_session(storage, session.session_id)
            if surface == "agent"
            else get_team_session(storage, session.session_id)
        )
        assert persisted is not None
        assert persisted.user_id == "before"
    finally:
        release_sanitizer.set()
        release_thread.join(timeout=1)
        await asyncio.gather(save, return_exceptions=True)
        mutation.cancel()
        await asyncio.gather(mutation, return_exceptions=True)
        storage.close()
