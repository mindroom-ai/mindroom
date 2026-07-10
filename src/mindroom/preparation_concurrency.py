"""Structured concurrency for independent pre-model preparation branches."""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True)
class _BranchOutcome[T]:
    value: T | None = None
    error: Exception | None = None


def _cleanup_agent_outcome[AgentT](
    outcome: _BranchOutcome[AgentT],
    *,
    cleanup_agent: Callable[[AgentT], None],
    on_secondary_error: Callable[[Exception], None],
) -> None:
    if outcome.value is not None:
        cleanup_agent(outcome.value)
    elif outcome.error is not None:
        on_secondary_error(outcome.error)


def _cleanup_completed_agent_task[AgentT](
    task: asyncio.Task[_BranchOutcome[AgentT]] | None,
    *,
    cleanup_agent: Callable[[AgentT], None],
    on_secondary_error: Callable[[Exception], None],
) -> None:
    if task is None or not task.done() or task.cancelled():
        return
    try:
        outcome = task.result()
    except BaseException:
        return
    _cleanup_agent_outcome(
        outcome,
        cleanup_agent=cleanup_agent,
        on_secondary_error=on_secondary_error,
    )


async def _capture_memory_branch[MemoryT](
    prepare_memory: Callable[[], Awaitable[MemoryT]],
    *,
    on_start: Callable[[], None],
    on_ready: Callable[[], None],
) -> _BranchOutcome[MemoryT]:
    on_start()
    try:
        return _BranchOutcome(value=await prepare_memory())
    except Exception as error:
        return _BranchOutcome(error=error)
    finally:
        on_ready()


async def _capture_agent_branch[AgentT](
    build_future: asyncio.Future[AgentT],
    *,
    cleanup_agent: Callable[[AgentT], None],
    on_ready: Callable[[], None],
    on_secondary_error: Callable[[Exception], None],
) -> _BranchOutcome[AgentT]:
    try:
        return _BranchOutcome(value=await asyncio.shield(build_future))
    except asyncio.CancelledError:
        try:
            unreturned_agent = await asyncio.shield(build_future)
        except Exception as error:
            on_secondary_error(error)
        else:
            cleanup_agent(unreturned_agent)
        raise
    except Exception as error:
        return _BranchOutcome(error=error)
    finally:
        on_ready()


def _submit_agent_build[AgentT](
    build_agent: Callable[[], AgentT],
    *,
    on_start: Callable[[], None],
) -> asyncio.Future[AgentT]:
    on_start()
    context = contextvars.copy_context()
    return asyncio.get_running_loop().run_in_executor(None, context.run, build_agent)


async def join_preparation_branches[MemoryT, AgentT](
    *,
    prepare_memory: Callable[[], Awaitable[MemoryT]],
    build_agent: Callable[[], AgentT],
    parallel: bool,
    cleanup_agent: Callable[[AgentT], None],
    on_memory_start: Callable[[], None],
    on_memory_ready: Callable[[], None],
    on_agent_start: Callable[[], None],
    on_agent_ready: Callable[[], None],
    on_secondary_agent_error: Callable[[Exception], None],
    task_name: str,
) -> tuple[MemoryT, AgentT]:
    """Run memory and sync agent preparation, preserving direct failure semantics."""
    agent_outcome: _BranchOutcome[AgentT] | None = None
    if parallel:
        build_future = _submit_agent_build(build_agent, on_start=on_agent_start)
        agent_task: asyncio.Task[_BranchOutcome[AgentT]] | None = None
        try:
            async with asyncio.TaskGroup() as task_group:
                agent_task = task_group.create_task(
                    _capture_agent_branch(
                        build_future,
                        cleanup_agent=cleanup_agent,
                        on_ready=on_agent_ready,
                        on_secondary_error=on_secondary_agent_error,
                    ),
                    name=f"agent_prepare:{task_name}",
                )
                memory_task = task_group.create_task(
                    _capture_memory_branch(
                        prepare_memory,
                        on_start=on_memory_start,
                        on_ready=on_memory_ready,
                    ),
                    name=f"memory_prepare:{task_name}",
                )
        except BaseException:
            _cleanup_completed_agent_task(
                agent_task,
                cleanup_agent=cleanup_agent,
                on_secondary_error=on_secondary_agent_error,
            )
            raise
        assert agent_task is not None
        agent_outcome = agent_task.result()
        try:
            memory_outcome = memory_task.result()
        except BaseException:
            _cleanup_agent_outcome(
                agent_outcome,
                cleanup_agent=cleanup_agent,
                on_secondary_error=on_secondary_agent_error,
            )
            raise
    else:
        memory_outcome = await _capture_memory_branch(
            prepare_memory,
            on_start=on_memory_start,
            on_ready=on_memory_ready,
        )
        if memory_outcome.error is None:
            build_future = _submit_agent_build(build_agent, on_start=on_agent_start)
            agent_outcome = await _capture_agent_branch(
                build_future,
                cleanup_agent=cleanup_agent,
                on_ready=on_agent_ready,
                on_secondary_error=on_secondary_agent_error,
            )

    if memory_outcome.error is not None:
        if agent_outcome is not None:
            _cleanup_agent_outcome(
                agent_outcome,
                cleanup_agent=cleanup_agent,
                on_secondary_error=on_secondary_agent_error,
            )
        raise memory_outcome.error
    if agent_outcome is not None and agent_outcome.error is not None:
        raise agent_outcome.error
    assert agent_outcome is not None
    return cast("MemoryT", memory_outcome.value), cast("AgentT", agent_outcome.value)
