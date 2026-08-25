"""Tests for per-turn async model-client ownership."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

from mindroom import ai

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


def _callbacks(
    holder: ai._AgentTurnHolder,
    *,
    retain_agent_runtime_state: bool = False,
) -> ai._AgentTurnCallbacks:
    return ai._build_agent_turn_callbacks(
        holder,
        agent_name="assistant",
        prompt="hello",
        current_prompt_is_structured=False,
        session_id="session-1",
        runtime_paths=cast("RuntimePaths", object()),
        config=cast("Any", object()),
        execution_identity=None,
        retain_agent_runtime_state=retain_agent_runtime_state,
    )


@pytest.mark.asyncio
async def test_agent_turn_finalizer_closes_live_final_attempt_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final attempt should close its prewarmed client before returning."""
    model = object()
    holder = ai._AgentTurnHolder(agent=cast("Any", SimpleNamespace(model=model)))
    close_client = AsyncMock()
    monkeypatch.setattr(ai, "aclose_anthropic_async_client", close_client)

    await _callbacks(holder).finalize_attempt(None)

    close_client.assert_awaited_once_with(model)


@pytest.mark.asyncio
async def test_agent_turn_finalizer_closes_released_continuation_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Releasing an attempt should retain its model until async finalization."""
    model = object()
    holder = ai._AgentTurnHolder(agent=cast("Any", SimpleNamespace(model=model)))
    close_client = AsyncMock()
    monkeypatch.setattr(ai, "aclose_anthropic_async_client", close_client)
    monkeypatch.setattr(ai, "close_agent_runtime_state_dbs", lambda *_args, **_kwargs: None)
    callbacks = _callbacks(holder)

    callbacks.release_attempt_entity(None)
    assert holder.agent is None
    await callbacks.finalize_attempt(None)

    close_client.assert_awaited_once_with(model)


@pytest.mark.asyncio
async def test_agent_turn_client_close_drains_before_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation should wait for an accepted client close to finish."""
    model = object()
    holder = ai._AgentTurnHolder(agent=cast("Any", SimpleNamespace(model=model)))
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    close_finished = asyncio.Event()

    async def _close_client(_model: object) -> None:
        close_started.set()
        await allow_close.wait()
        close_finished.set()

    monkeypatch.setattr(ai, "aclose_anthropic_async_client", _close_client)
    task = asyncio.create_task(_callbacks(holder).finalize_attempt(None))
    await close_started.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert close_finished.is_set()


@pytest.mark.asyncio
async def test_reusable_agent_turn_does_not_close_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-owned reusable agent should retain its async client."""
    model = object()
    holder = ai._AgentTurnHolder(agent=cast("Any", SimpleNamespace(model=model)))
    close_client = AsyncMock()
    monkeypatch.setattr(ai, "aclose_anthropic_async_client", close_client)

    await _callbacks(holder, retain_agent_runtime_state=True).finalize_attempt(None)

    close_client.assert_not_awaited()
