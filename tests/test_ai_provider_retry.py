"""Provider retries preserve the real streamed turn and its durable history."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar

import pytest
from agno.tools import Toolkit

from mindroom.ai import stream_agent_response
from mindroom.cancellation import request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.llm.errors import ProviderUnavailable
from mindroom.llm.events import ContentDelta
from mindroom.llm.provider import TextDelta
from mindroom.providers.pipeline import retry as provider_stream_retry
from mindroom.providers.pipeline.compose import wrap_provider
from mindroom.session_storage.agent_storage import create_session_storage
from tests.conftest import bind_runtime_paths, make_turn_context, runtime_paths_for, test_runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.scripted_agent_helpers import install_agent_model, registered_toolkit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence
    from pathlib import Path

    from mindroom.ai import AIStreamChunk
    from mindroom.llm.runs import AnyRunRecord

SESSION_ID = "retry-session"


class _StepTools(Toolkit):
    """One tool whose effects the tests count, so a replayed call would show."""

    actions: ClassVar[list[int]] = []

    def __init__(self) -> None:
        super().__init__(name="steps", tools=[self.record_step])

    def record_step(self, step: int) -> str:
        """Perform one recorded action.

        Args:
            step: The step to perform.

        """
        type(self).actions.append(step)
        return f"step-{step}-done"


@pytest.fixture
def steps() -> Iterator[type[_StepTools]]:
    """Register the step toolkit for the test."""
    _StepTools.actions.clear()
    with registered_toolkit("retry_steps", _StepTools):
        yield _StepTools


def _overload() -> Await:
    """The provider fails the request with a retryable overload before the model said anything."""

    async def fail() -> None:
        msg = "overloaded"
        raise ProviderUnavailable(msg, provider="Synthetic", status_code=529)

    return Await(fail)


def _install_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turns: Sequence[Sequence[ScriptStep]],
) -> tuple[Config, ScriptedModel]:
    """Configure the worker agent on the scripted model inside the production request pipeline."""
    config = bind_runtime_paths(
        Config(
            agents={"worker": AgentConfig(display_name="Worker", tools=["retry_steps"])},
            models={"default": ModelConfig(provider="openai", id="test-model", api_key="test-key")},
        ),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "none"
    model = ScriptedModel([list(turn) for turn in turns])
    install_agent_model(
        monkeypatch,
        wrap_provider(model, idle_timeout_seconds=None, media_fallback_prompt="", request_log=None),
    )
    return config, model


def _stream(config: Config, run_id: str) -> AsyncIterator[AIStreamChunk]:
    return stream_agent_response(
        make_turn_context("worker", session_id=SESSION_ID, run_id=run_id, requester_id="@user:localhost"),
        prompt="Perform both steps." if run_id == "first" else "Give a follow-up answer.",
        runtime_paths=runtime_paths_for(config),
        config=config,
        show_tool_calls=False,
    )


def _text(chunks: Sequence[AIStreamChunk]) -> str:
    return "".join(chunk.text for chunk in chunks if isinstance(chunk, ContentDelta))


def _stored_runs(config: Config) -> list[AnyRunRecord]:
    storage = create_session_storage("worker", config, runtime_paths_for(config), execution_identity=None)
    session = storage.load_session(SESSION_ID, "agent")
    assert session is not None
    return session.runs


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["recover", "user_stop", "sync_restart"])
async def test_post_tool_backoff_preserves_turn_history_and_followup(
    finish: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: type[_StepTools],
) -> None:
    """Retry or cancel the third model request without replaying either completed tool."""
    waiting, release = asyncio.Event(), asyncio.Event()

    async def wait(_delay: float) -> None:
        waiting.set()
        await release.wait()

    monkeypatch.setattr(provider_stream_retry, "asyncio", SimpleNamespace(sleep=wait, timeout=asyncio.timeout))
    turns: list[list[ScriptStep]] = [
        calls(tool_call("record_step", "call_step_1", step=1)),
        calls(tool_call("record_step", "call_step_2", step=2)),
        [_overload()],
    ]
    if finish == "recover":
        turns.append(answer("Recovered"))
    turns.append(answer("Follow-up"))
    config, model = _install_agent(tmp_path, monkeypatch, turns)

    async def consume() -> list[AIStreamChunk]:
        return [chunk async for chunk in _stream(config, "first")]

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(10):
            await waiting.wait()
            assert steps.actions == [1, 2]
            assert len(model.requests) == 3
            if finish == "recover":
                release.set()
                chunks = await task
                assert _text(chunks) == "Recovered"
                assert model.requests[2] == model.requests[3]
            else:
                request_task_cancel(task, cancel_source=finish)
                with pytest.raises(asyncio.CancelledError, match=finish):
                    await task
                release.set()
            runs = _stored_runs(config)
            assert len(runs) == 1
            if finish != "recover":
                assert runs[0].metadata is not None
                assert runs[0].metadata["mindroom_original_status"] == "cancelled"
            saved = " ".join(str(message.content) for message in runs[0].messages)
            assert "step-1-done" in saved
            assert "step-2-done" in saved
            before_followup = len(model.requests)
            followup = [chunk async for chunk in _stream(config, "followup")]
            assert _text(followup) == "Follow-up"
            assert len(model.requests) == before_followup + 1
            assert len(_stored_runs(config)) == 2
            assert steps.actions == [1, 2]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_partial_stream_failure_stays_one_durable_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: type[_StepTools],
) -> None:
    """A request that fails after visible output is not retried, and the turn keeps its partial output once."""
    del steps
    config, model = _install_agent(
        tmp_path,
        monkeypatch,
        [[TextDelta("Partial"), _overload()], answer("Must not run")],
    )

    chunks = [chunk async for chunk in _stream(config, "first")]

    assert len(model.requests) == 1
    assert _text(chunks).count("Partial") == 1
    runs = _stored_runs(config)
    assert len(runs) == 1
    assert runs[0].metadata is not None
    assert runs[0].metadata["mindroom_original_status"] == "error"
    assert "Partial" in str(runs[0].content)
