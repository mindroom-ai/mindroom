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
from mindroom.llm.provider import TextDelta
from mindroom.providers.pipeline import wrapped as provider_attempts
from mindroom.providers.pipeline.compose import wrap_provider
from mindroom.providers.pipeline.request_log import RequestLog
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    make_turn_context,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.scripted_agent_helpers import raising, registered_toolkit, stored_runs, streamed_text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence
    from pathlib import Path

    from mindroom.ai import AIStreamChunk

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
    return raising(ProviderUnavailable("overloaded", provider="Synthetic", status_code=529))


def _install_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turns: Sequence[Sequence[ScriptStep]],
) -> tuple[Config, ScriptedModel]:
    """Configure the worker agent on the scripted model inside the production request pipeline."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"worker": AgentConfig(display_name="Worker", tools=["retry_steps"])},
            models={"default": ModelConfig(provider="openai", id="test-model", api_key="test-key")},
        ),
        runtime_paths,
    )
    config.memory.backend = "none"
    model = ScriptedModel([list(turn) for turn in turns])
    request_log = RequestLog(
        model_name="default",
        configured_provider="openai",
        debug=config.debug,
        storage_root=runtime_paths.storage_root,
    )
    install_model_providers(
        monkeypatch,
        {"default": wrap_provider(model, idle_timeout_seconds=None, media_fallback_prompt="", request_log=request_log)},
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

    # The attempt runner waits out the stream retry's backoff.
    monkeypatch.setattr(provider_attempts, "asyncio", SimpleNamespace(sleep=wait))
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
                assert "".join(streamed_text(chunks)) == "Recovered"
                assert model.requests[2] == model.requests[3]
            else:
                request_task_cancel(task, cancel_source=finish)
                with pytest.raises(asyncio.CancelledError, match=finish):
                    await task
                release.set()
            runs = stored_runs(config, runtime_paths_for(config), SESSION_ID, agent_name="worker")
            assert len(runs) == 1
            if finish != "recover":
                assert runs[0].metadata is not None
                assert runs[0].metadata["mindroom_original_status"] == "cancelled"
            saved = " ".join(str(message.content) for message in runs[0].messages)
            assert "step-1-done" in saved
            assert "step-2-done" in saved
            before_followup = len(model.requests)
            followup = [chunk async for chunk in _stream(config, "followup")]
            assert "".join(streamed_text(followup)) == "Follow-up"
            assert len(model.requests) == before_followup + 1
            assert len(stored_runs(config, runtime_paths_for(config), SESSION_ID, agent_name="worker")) == 2
            assert steps.actions == [1, 2]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("steps")
async def test_partial_stream_failure_stays_one_durable_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that fails after visible output is not retried, and the turn keeps its partial output once."""
    config, model = _install_agent(
        tmp_path,
        monkeypatch,
        [[TextDelta("Partial"), _overload()], answer("Must not run")],
    )

    chunks = [chunk async for chunk in _stream(config, "first")]

    assert len(model.requests) == 1
    assert "".join(streamed_text(chunks)).count("Partial") == 1
    runs = stored_runs(config, runtime_paths_for(config), SESSION_ID, agent_name="worker")
    assert len(runs) == 1
    assert runs[0].metadata is not None
    assert runs[0].metadata["mindroom_original_status"] == "error"
    assert "Partial" in str(runs[0].content)
