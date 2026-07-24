"""Tests for concurrent prompt preparation."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

import mindroom.pre_model_preparation as pre_model_preparation_module
from mindroom.config.main import ResolvedRuntimeModel
from mindroom.memory import MemoryPromptParts
from mindroom.pre_model_preparation import prepare_prompt_branches


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_branch", ["memory", "agent", "both", "memory_cancelled"])
async def test_prepare_prompt_branches_propagates_failure_directly(
    monkeypatch: pytest.MonkeyPatch,
    failing_branch: str,
) -> None:
    """Either branch failure keeps its original exception and memory-first precedence."""
    rendezvous = threading.Barrier(2)
    memory_error = (
        asyncio.CancelledError("memory cancelled")
        if failing_branch == "memory_cancelled"
        else RuntimeError("memory failed")
    )
    agent_error = RuntimeError("agent failed")
    built_agent = MagicMock()
    runtime_model = ResolvedRuntimeModel(model_name="default", context_window=None)
    close_unreturned = MagicMock()
    test_logger = MagicMock()

    async def memory_branch() -> MemoryPromptParts:
        await asyncio.to_thread(rendezvous.wait, 5.0)
        if failing_branch in {"memory", "both", "memory_cancelled"}:
            raise memory_error
        return MemoryPromptParts()

    def agent_branch() -> tuple[ResolvedRuntimeModel, MagicMock]:
        rendezvous.wait(5.0)
        if failing_branch in {"agent", "both"}:
            raise agent_error
        return runtime_model, built_agent

    monkeypatch.setattr(pre_model_preparation_module, "close_agent_runtime_state_dbs", close_unreturned)
    monkeypatch.setattr(pre_model_preparation_module, "logger", test_logger)

    expected_error_type = asyncio.CancelledError if failing_branch == "memory_cancelled" else RuntimeError
    with pytest.raises(expected_error_type) as raised:
        await prepare_prompt_branches(
            prepare_memory=memory_branch,
            build_agent=agent_branch,
            agent_name="general",
            shared_scope_storage=None,
            pipeline_timing=None,
        )

    expected_error = agent_error if failing_branch == "agent" else memory_error
    assert raised.value is expected_error
    if failing_branch in {"memory", "memory_cancelled"}:
        close_unreturned.assert_called_once_with(built_agent, shared_scope_storage=None)
    else:
        close_unreturned.assert_not_called()
    if failing_branch == "both":
        test_logger.error.assert_called_once()
    else:
        test_logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_prompt_branches_agent_failure_cancels_memory_sibling() -> None:
    """File-mode agent failure cancels and joins its async memory sibling."""
    memory_started = threading.Event()
    memory_cleaned = asyncio.Event()
    agent_error = RuntimeError("agent failed")

    async def blocked_memory() -> MemoryPromptParts:
        memory_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            memory_cleaned.set()

    def failed_agent() -> tuple[ResolvedRuntimeModel, MagicMock]:
        if not memory_started.wait(5.0):
            msg = "timed out waiting for memory preparation"
            raise TimeoutError(msg)
        raise agent_error

    baseline_tasks = set(asyncio.all_tasks())
    with pytest.raises(RuntimeError) as raised:
        await prepare_prompt_branches(
            prepare_memory=blocked_memory,
            build_agent=failed_agent,
            agent_name="general",
            shared_scope_storage=None,
            pipeline_timing=None,
            cancel_memory_on_agent_failure=True,
        )

    assert raised.value is agent_error
    assert memory_cleaned.is_set()
    await asyncio.sleep(0)
    leaked_tasks = {task for task in asyncio.all_tasks() - baseline_tasks if not task.done()}
    assert leaked_tasks == set()


@pytest.mark.asyncio
async def test_prepare_prompt_branches_memory_failure_joins_agent_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory failure waits for uninterruptible construction and closes its agent."""
    agent_started = threading.Event()
    agent_release = threading.Event()
    agent_finished = threading.Event()
    memory_failed = asyncio.Event()
    memory_error = RuntimeError("memory failed")
    runtime_model = ResolvedRuntimeModel(model_name="default", context_window=None)
    built_agent = MagicMock()
    close_unreturned = MagicMock()

    async def failed_memory() -> MemoryPromptParts:
        assert await asyncio.to_thread(agent_started.wait, 5.0)
        memory_failed.set()
        raise memory_error

    def blocked_agent() -> tuple[ResolvedRuntimeModel, MagicMock]:
        agent_started.set()
        if not agent_release.wait(5.0):
            msg = "timed out waiting to release agent construction"
            raise TimeoutError(msg)
        agent_finished.set()
        return runtime_model, built_agent

    monkeypatch.setattr(pre_model_preparation_module, "close_agent_runtime_state_dbs", close_unreturned)

    prepare_task = asyncio.create_task(
        prepare_prompt_branches(
            prepare_memory=failed_memory,
            build_agent=blocked_agent,
            agent_name="general",
            shared_scope_storage=None,
            pipeline_timing=None,
            cancel_memory_on_agent_failure=True,
        ),
    )
    try:
        await asyncio.wait_for(memory_failed.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert not prepare_task.done()
        agent_release.set()
        with pytest.raises(RuntimeError) as raised:
            await prepare_task
    finally:
        agent_release.set()

    assert raised.value is memory_error
    assert agent_finished.is_set()
    close_unreturned.assert_called_once_with(built_agent, shared_scope_storage=None)


@pytest.mark.asyncio
async def test_prepare_prompt_branches_preserves_caller_owned_agent_on_memory_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed memory branch must not close a reusable agent owned by its caller."""
    built_agent = MagicMock()
    runtime_model = ResolvedRuntimeModel(model_name="default", context_window=None)
    close_unreturned = MagicMock()

    async def memory_branch() -> MemoryPromptParts:
        await asyncio.sleep(0)
        msg = "memory failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(pre_model_preparation_module, "close_agent_runtime_state_dbs", close_unreturned)

    with pytest.raises(RuntimeError, match="memory failed"):
        await prepare_prompt_branches(
            prepare_memory=memory_branch,
            build_agent=lambda: (runtime_model, built_agent),
            agent_name="general",
            shared_scope_storage=None,
            pipeline_timing=None,
            caller_owned_agent=built_agent,
        )

    close_unreturned.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_outcome", "caller_owned"),
    [
        ("pending", False),
        ("finished", False),
        ("fails", False),
        ("cancelled", False),
        ("finished", True),
    ],
)
async def test_prepare_prompt_branches_cancellation_settles_agent_build(  # noqa: C901, PLR0915
    monkeypatch: pytest.MonkeyPatch,
    agent_outcome: str,
    caller_owned: bool,
) -> None:
    """Cancellation drains construction without closing caller-owned agents or leaking tasks."""
    memory_started = asyncio.Event()
    memory_cleaned = asyncio.Event()
    agent_started = threading.Event()
    agent_release = threading.Event()
    agent_finished = threading.Event()
    built_agent = MagicMock()
    runtime_model = ResolvedRuntimeModel(model_name="default", context_window=None)
    close_unreturned = MagicMock()
    test_logger = MagicMock()

    async def blocked_memory() -> MemoryPromptParts:
        memory_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            memory_cleaned.set()

    def blocked_agent() -> tuple[ResolvedRuntimeModel, MagicMock]:
        agent_started.set()
        if agent_outcome == "finished":
            agent_finished.set()
            return runtime_model, built_agent
        if not agent_release.wait(5.0):
            msg = "timed out waiting to release cancelled agent construction"
            raise TimeoutError(msg)
        agent_finished.set()
        if agent_outcome == "fails":
            msg = "agent failed after cancellation"
            raise RuntimeError(msg)
        if agent_outcome == "cancelled":
            raise asyncio.CancelledError
        return runtime_model, built_agent

    monkeypatch.setattr(pre_model_preparation_module, "close_agent_runtime_state_dbs", close_unreturned)
    monkeypatch.setattr(pre_model_preparation_module, "logger", test_logger)

    baseline_tasks = set(asyncio.all_tasks())
    prepare_task = asyncio.create_task(
        prepare_prompt_branches(
            prepare_memory=blocked_memory,
            build_agent=blocked_agent,
            agent_name="general",
            shared_scope_storage=None,
            pipeline_timing=None,
            caller_owned_agent=built_agent if caller_owned else None,
        ),
    )
    try:
        await asyncio.wait_for(memory_started.wait(), timeout=1.0)
        assert await asyncio.to_thread(agent_started.wait, 1.0)
        if agent_outcome == "finished":
            assert await asyncio.to_thread(agent_finished.wait, 1.0)
        prepare_task.cancel()
        await asyncio.wait_for(memory_cleaned.wait(), timeout=1.0)
        await asyncio.sleep(0)
        if agent_outcome != "finished":
            assert not prepare_task.done()
        if agent_outcome == "pending":
            prepare_task.cancel()
            await asyncio.sleep(0)
            assert not prepare_task.done()

        agent_release.set()
        with pytest.raises(asyncio.CancelledError):
            await prepare_task
    finally:
        agent_release.set()

    assert agent_finished.is_set()
    if agent_outcome in {"fails", "cancelled"} or caller_owned:
        close_unreturned.assert_not_called()
    else:
        close_unreturned.assert_called_once_with(built_agent, shared_scope_storage=None)
    if agent_outcome == "fails":
        test_logger.error.assert_called_once()
    else:
        test_logger.error.assert_not_called()
    await asyncio.sleep(0)
    leaked_tasks = {task for task in asyncio.all_tasks() - baseline_tasks if not task.done()}
    assert leaked_tasks == set()
