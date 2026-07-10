"""Structured concurrency for independent pre-model preparation branches."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(frozen=True)
class _BranchOutcome[T]:
    value: T | None = None
    error: Exception | None = None


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
    build_agent: Callable[[], AgentT],
    *,
    cleanup_agent: Callable[[AgentT], None],
    on_start: Callable[[], None],
    on_ready: Callable[[], None],
    on_secondary_error: Callable[[Exception], None],
    task_name: str,
) -> _BranchOutcome[AgentT]:
    on_start()
    build_task = asyncio.create_task(
        asyncio.to_thread(build_agent),
        name=f"agent_build:{task_name}",
    )
    try:
        return _BranchOutcome(value=await asyncio.shield(build_task))
    except asyncio.CancelledError:
        try:
            unreturned_agent = await asyncio.shield(build_task)
        except Exception as error:
            on_secondary_error(error)
        else:
            cleanup_agent(unreturned_agent)
        raise
    except Exception as error:
        return _BranchOutcome(error=error)
    finally:
        on_ready()


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
        async with asyncio.TaskGroup() as task_group:
            memory_task = task_group.create_task(
                _capture_memory_branch(
                    prepare_memory,
                    on_start=on_memory_start,
                    on_ready=on_memory_ready,
                ),
                name=f"memory_prepare:{task_name}",
            )
            agent_task = task_group.create_task(
                _capture_agent_branch(
                    build_agent,
                    cleanup_agent=cleanup_agent,
                    on_start=on_agent_start,
                    on_ready=on_agent_ready,
                    on_secondary_error=on_secondary_agent_error,
                    task_name=task_name,
                ),
                name=f"agent_prepare:{task_name}",
            )
        memory_outcome = memory_task.result()
        agent_outcome = agent_task.result()
    else:
        memory_outcome = await _capture_memory_branch(
            prepare_memory,
            on_start=on_memory_start,
            on_ready=on_memory_ready,
        )
        if memory_outcome.error is None:
            agent_outcome = await _capture_agent_branch(
                build_agent,
                cleanup_agent=cleanup_agent,
                on_start=on_agent_start,
                on_ready=on_agent_ready,
                on_secondary_error=on_secondary_agent_error,
                task_name=task_name,
            )

    if memory_outcome.error is not None:
        if agent_outcome is not None and agent_outcome.value is not None:
            cleanup_agent(agent_outcome.value)
        if agent_outcome is not None and agent_outcome.error is not None:
            on_secondary_agent_error(agent_outcome.error)
        raise memory_outcome.error
    if agent_outcome is not None and agent_outcome.error is not None:
        raise agent_outcome.error
    assert agent_outcome is not None
    return cast("MemoryT", memory_outcome.value), cast("AgentT", agent_outcome.value)
