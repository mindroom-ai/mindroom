"""Regression tests for synchronous Agno session persistence atomicity."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.metrics import MessageMetrics, RunMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
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
        Message(role="user", content="question", metrics=MessageMetrics(input_tokens=3)),
    ]
    session_data = {
        "session_state": {"current_run_id": "run"},
        "user_data": {"level": 2},
        "memories": ["remembered"],
    }
    summary = SessionSummary(summary="summary", topics=["topic"])
    if surface == "agent":
        return Agent(db=storage, telemetry=False), AgentSession(
            session_id=session_id,
            agent_id="agent",
            user_id="before",
            agent_data={"model": "model"},
            session_data=session_data,
            metadata={"scope": {"id": 1}},
            summary=summary,
            created_at=int(time.time()),
            runs=[
                RunOutput(
                    run_id="run",
                    agent_id="agent",
                    session_id=session_id,
                    messages=messages,
                    metrics=RunMetrics(total_tokens=5, additional_metrics={"quality": 1}),
                    metadata={"run": "metadata"},
                ),
            ],
        )
    return Team(db=storage, members=[], telemetry=False), TeamSession(
        session_id=session_id,
        team_id="team",
        user_id="before",
        team_data={"mode": "coordinate"},
        session_data=session_data,
        metadata={"scope": {"id": 1}},
        summary=summary,
        created_at=int(time.time()),
        runs=[
            TeamRunOutput(
                run_id="run",
                team_id="team",
                session_id=session_id,
                messages=messages,
                metrics=RunMetrics(total_tokens=5, additional_metrics={"quality": 1}),
                metadata={"run": "metadata"},
            ),
        ],
    )


@pytest.mark.parametrize("surface", ["agent", "team"])
@pytest.mark.asyncio
async def test_live_session_cannot_mutate_while_sync_save_is_copying(  # noqa: PLR0915
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
        assert not save.done()
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
        assert session.user_id == "after"
        assert persisted.user_id == "before"
        assert session.session_data is not None
        assert session.session_data["session_state"] == {}
        assert persisted.session_data == {
            "session_state": {},
            "user_data": {"level": 2},
            "memories": ["remembered"],
        }
        assert persisted.metadata == {"scope": {"id": 1}}
        assert persisted.summary is not None
        assert persisted.summary.summary == "summary"
        assert persisted.summary.topics == ["topic"]
        assert getattr(persisted, "agent_data", None) == ({"model": "model"} if surface == "agent" else None)
        assert getattr(persisted, "team_data", None) == ({"mode": "coordinate"} if surface == "team" else None)
        assert persisted.runs is not None
        assert persisted.runs[0].metrics is not None
        assert persisted.runs[0].metrics.total_tokens == 5
        assert persisted.runs[0].metrics.additional_metrics == {"quality": 1}
        assert persisted.runs[0].metadata == {"run": "metadata"}
        assert persisted.runs[0].messages is not None
        assert [message.role for message in persisted.runs[0].messages] == ["user"]
        assert persisted.runs[0].messages[0].metrics.input_tokens == 3
    finally:
        release_sanitizer.set()
        release_thread.join(timeout=1)
        await asyncio.gather(save, return_exceptions=True)
        mutation.cancel()
        await asyncio.gather(mutation, return_exceptions=True)
        storage.close()
