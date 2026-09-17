"""Provider retries preserve the real streamed turn and its durable history."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.run.agent import RunContentEvent

from mindroom import provider_stream_retry
from mindroom.agent_storage import get_agent_session
from mindroom.ai import stream_agent_response
from mindroom.cancellation import request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from tests.conftest import bind_runtime_paths, make_turn_context, runtime_paths_for, test_runtime_paths
from tests.test_openai_responses_stream import _created, _event, _response
from tests.test_provider_stream_retry import _answer as _chat_answer
from tests.test_provider_stream_retry import _chunk, _model, _overload, _Provider
from tests.test_responses_stream_retry import _answer as _responses_answer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.run.agent import RunOutput

    from mindroom.ai import AIStreamChunk
    from mindroom.openai_models import MindRoomOpenAIChat, MindRoomOpenAIResponses

type _API = Literal["chat_completions", "responses"]


def _tool_stream(api: _API, step: int) -> str:
    call_id = f"call_step_{step}"
    arguments = json.dumps({"step": step})
    if api == "chat_completions":
        return (
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "record_step",
                                "arguments": arguments,
                            },
                        },
                    ],
                },
            )
            + _chunk({}, "tool_calls")
            + "data: [DONE]\n\n"
        )
    item = {
        "type": "function_call",
        "id": f"fc_{step}",
        "call_id": call_id,
        "name": "record_step",
        "arguments": arguments,
        "status": "completed",
    }
    return (
        _created(f"resp_step_{step}")
        + _event("response.output_item.added", output_index=0, item={**item, "arguments": "", "status": "in_progress"})
        + _event("response.function_call_arguments.delta", output_index=0, item_id=f"fc_{step}", delta=arguments)
        + _event("response.output_item.done", output_index=0, item=item)
        + _event("response.completed", response=_response(f"resp_step_{step}", "completed", [item]))
    )


def _answer(api: _API, content: str) -> str:
    return _chat_answer(content) if api == "chat_completions" else _responses_answer(content)


def _install_agent(
    model: MindRoomOpenAIChat | MindRoomOpenAIResponses,
    api: _API,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actions: list[int],
) -> tuple[Config, list[Agent]]:
    config = bind_runtime_paths(
        Config(
            agents={"worker": AgentConfig(display_name="Worker")},
            models={"default": ModelConfig(provider="openai", id="test-model", api=api, api_key="test-key")},
        ),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "none"
    agents: list[Agent] = []

    def record_step(step: int) -> str:
        """Perform one recorded action."""
        actions.append(step)
        return f"step-{step}-done"

    def create_agent(*_args: object, **kwargs: object) -> Agent:
        database = kwargs["history_storage"]
        assert isinstance(database, SqliteDb)
        agent = Agent(
            id="worker",
            name="worker",
            model=model,
            tools=[record_step],
            db=database,
            session_id="retry-session",
            add_history_to_context=True,
            store_history_messages=False,
            telemetry=False,
            retries=0,
        )
        agents.append(agent)
        return agent

    monkeypatch.setattr("mindroom.ai.create_agent", create_agent)
    return config, agents


def _stream(config: Config, run_id: str) -> AsyncIterator[AIStreamChunk]:
    return stream_agent_response(
        make_turn_context("worker", session_id="retry-session", run_id=run_id, requester_id="@user:localhost"),
        prompt="Perform both steps." if run_id == "first" else "Give a follow-up answer.",
        runtime_paths=runtime_paths_for(config),
        config=config,
        show_tool_calls=False,
    )


def _stored_runs(agent: Agent) -> list[RunOutput]:
    assert isinstance(agent.db, SqliteDb)
    storage = SqliteDb(db_file=agent.db.db_file, session_table=agent.db.session_table_name)
    try:
        session = get_agent_session(storage, "retry-session")
        assert session is not None
        assert session.runs is not None
        return session.runs
    finally:
        storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat_completions", "responses"])
@pytest.mark.parametrize("finish", ["recover", "user_stop", "sync_restart"])
async def test_post_tool_backoff_preserves_turn_history_and_followup(  # noqa: PLR0915
    api: _API,
    finish: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry or cancel the third model request without replaying either completed tool."""
    waiting, release = asyncio.Event(), asyncio.Event()

    async def wait(_delay: float) -> None:
        waiting.set()
        await release.wait()

    monkeypatch.setattr(provider_stream_retry, "asyncio", SimpleNamespace(sleep=wait))
    overload = (_created() if api == "responses" else "") + _overload()
    attempts = [_tool_stream(api, 1), _tool_stream(api, 2), overload]
    if finish == "recover":
        attempts.append(_answer(api, "Recovered"))
    attempts.append(_answer(api, "Follow-up"))
    provider = _Provider(attempts)
    actions: list[int] = []
    async with _model(provider, tmp_path, api=api) as model:
        config, agents = _install_agent(model, api, tmp_path, monkeypatch, actions)

        async def consume() -> list[AIStreamChunk]:
            return [chunk async for chunk in _stream(config, "first")]

        task = asyncio.create_task(consume())
        try:
            async with asyncio.timeout(10):
                await waiting.wait()
                assert actions == [1, 2]
                assert len(provider.requests) == 3
                if finish == "recover":
                    release.set()
                    chunks = await task
                    assert (
                        "".join(chunk.content or "" for chunk in chunks if isinstance(chunk, RunContentEvent))
                        == "Recovered"
                    )
                    assert provider.requests[2] == provider.requests[3]
                else:
                    request_task_cancel(task, cancel_source=finish)
                    with pytest.raises(asyncio.CancelledError, match=finish):
                        await task
                    release.set()
                assert len(agents) == 1
                runs = _stored_runs(agents[0])
                assert len(runs) == 1
                if finish != "recover":
                    assert runs[0].metadata["mindroom_original_status"] == "cancelled"
                saved = " ".join(str(message.content) for message in runs[0].messages or [])
                assert "step-1-done" in saved
                assert "step-2-done" in saved
                before_followup = len(provider.requests)
                followup = [chunk async for chunk in _stream(config, "followup")]
                assert (
                    "".join(chunk.content or "" for chunk in followup if isinstance(chunk, RunContentEvent))
                    == "Follow-up"
                )
                assert len(provider.requests) == before_followup + 1
                assert len(_stored_runs(agents[-1])) == 2
                assert actions == [1, 2]
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat_completions", "responses"])
async def test_partial_stream_failure_stays_one_durable_turn(
    api: _API,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer turn driver retains partial output without starting a fresh model run."""
    partial = (
        _chunk({"content": "Partial"}) if api == "chat_completions" else _responses_answer("Partial", complete=False)
    )
    provider = _Provider([partial + _overload(), _answer(api, "Must not run")])
    async with _model(provider, tmp_path, api=api) as model:
        config, agents = _install_agent(model, api, tmp_path, monkeypatch, [])
        chunks = [chunk async for chunk in _stream(config, "first")]

    assert len(provider.requests) == 1
    assert len(agents) == 1
    text = "".join(chunk.content or "" for chunk in chunks if isinstance(chunk, RunContentEvent))
    assert text.count("Partial") == 1
    runs = _stored_runs(agents[0])
    assert len(runs) == 1
    assert runs[0].metadata["mindroom_original_status"] == "error"
    assert "Partial" in str(runs[0].content)
