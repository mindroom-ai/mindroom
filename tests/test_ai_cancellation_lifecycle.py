"""Real AI streams preserve cancellation ownership when consumers switch tasks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.run.agent import RunContentEvent, RunOutput
from agno.run.base import RunStatus
from openai import AsyncOpenAI

from mindroom import agno_session_persistence_patch as persistence
from mindroom.agent_storage import get_agent_session
from mindroom.ai import stream_agent_response
from mindroom.openai_models import MindRoomOpenAIChat
from tests.bot_helpers import AgentBotTestBase
from tests.conftest import make_turn_context, runtime_paths_for
from tests.test_participation_disabled import _provider_response

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["exhaust", "close"])
async def test_ai_stream_can_finish_in_another_task(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finish: str,
) -> None:
    """A parent pull must not leak context, and child close must drain its raw cancelled save."""
    config = AgentBotTestBase._config_for_storage(tmp_path)
    config.memory.backend = "none"
    runtime_paths = runtime_paths_for(config)
    session_id = "cross-task-session"
    run_id = "cross-task-run"
    save_started = asyncio.Event()
    release_save = asyncio.Event()
    background: list[asyncio.Task[object]] = []
    agents: list[Agent] = []
    original_save = persistence._offload_sync_save

    async def delayed_save(
        lane: persistence._PersistenceLane,
        save: Callable[..., object],
        owner: object,
        payload: object,
        *args: object,
    ) -> None:
        if isinstance(payload, RunOutput) and payload.status == RunStatus.cancelled:
            task = asyncio.current_task()
            assert task is not None
            background.append(task)
            save_started.set()
            await release_save.wait()
        await original_save(lane, save, owner, payload, *args)

    def provider(request: httpx.Request) -> httpx.Response:
        return _provider_response(json.loads(request.content), {"role": "assistant", "content": "Visible answer"})

    monkeypatch.setattr(persistence, "_offload_sync_save", delayed_save)
    parent_token = persistence._CANCELLATION_OWNER.set(None)
    child: asyncio.Task[object] | None = None
    stream = None
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
            model = MindRoomOpenAIChat(
                id="test",
                api_key="test-key",
                async_client=AsyncOpenAI(api_key="test-key", http_client=http_client, max_retries=0),
            )

            def create_agent(*_args: object, **kwargs: object) -> Agent:
                database = kwargs["history_storage"]
                assert isinstance(database, SqliteDb)
                assert kwargs["session_id"] == session_id
                agent = Agent(
                    id="calculator",
                    name="calculator",
                    model=model,
                    db=database,
                    session_id=session_id,
                    telemetry=False,
                    retries=0,
                )
                agents.append(agent)
                return agent

            monkeypatch.setattr("mindroom.ai.create_agent", create_agent)
            stream = stream_agent_response(
                make_turn_context("calculator", session_id=session_id, run_id=run_id, requester_id="@user:localhost"),
                prompt="Please answer.",
                runtime_paths=runtime_paths,
                config=config,
                show_tool_calls=False,
            )
            assert isinstance(stream, AsyncGenerator)
            async with asyncio.timeout(5):
                first = await anext(stream)
            assert isinstance(first, RunContentEvent)
            assert first.content == "Visible answer"
            owner_after_parent_pull = persistence._CANCELLATION_OWNER.get()

            async def finish_in_child() -> object:
                if finish == "close":
                    await stream.aclose()
                    return None
                return [chunk async for chunk in stream]

            child = asyncio.create_task(finish_in_child())
            close_waited = True
            async with asyncio.timeout(5):
                if finish == "close":
                    await save_started.wait()
                    await asyncio.sleep(0)
                    close_waited = not child.done()
                    release_save.set()
                result = (await asyncio.gather(child, return_exceptions=True))[0]
            drained_at_child_return = all(task.done() for task in background)
            owner_after_child = persistence._CANCELLATION_OWNER.get()

        assert owner_after_parent_pull is None, "Cancellation owner leaked into the parent at a public yield"
        assert owner_after_child is None
        assert not isinstance(result, BaseException), repr(result)
        assert close_waited, "Cross-task close returned before its accepted cancellation save"
        assert drained_at_child_return
        assert len(background) == (1 if finish == "close" else 0)
        assert len(agents) == 1
        assert isinstance(agents[0].db, SqliteDb)
        storage = SqliteDb(db_file=agents[0].db.db_file, session_table=agents[0].db.session_table_name)
        try:
            session = get_agent_session(storage, session_id)
            assert session is not None
            assert session.runs is not None
            assert len(session.runs) == 1
            assert session.runs[0].status == (RunStatus.cancelled if finish == "close" else RunStatus.completed)
        finally:
            storage.close()
    finally:
        release_save.set()
        if child is not None:
            await asyncio.gather(child, return_exceptions=True)
        await asyncio.gather(*background, return_exceptions=True)
        if isinstance(stream, AsyncGenerator):
            await stream.aclose()
        persistence._CANCELLATION_OWNER.reset(parent_token)
