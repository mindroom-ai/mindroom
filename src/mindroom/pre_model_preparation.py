"""Concurrent pre-model preparation for agent turns."""

from __future__ import annotations

import asyncio
import contextvars
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.claude_prompt_cache import aclose_anthropic_async_client
from mindroom.history.runtime import close_agent_runtime_state_dbs
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.agent import Agent
    from agno.db.base import BaseDb

    from mindroom.config.main import ResolvedRuntimeModel
    from mindroom.memory import MemoryPromptParts
    from mindroom.timing import DispatchPipelineTiming

# Keep extraction behavior-neutral for per-logger routing and emitted logger fields.
logger = get_logger("mindroom.ai")


def _mark_pipeline_timing(pipeline_timing: DispatchPipelineTiming | None, label: str) -> None:
    if pipeline_timing is not None:
        pipeline_timing.mark(label)


def _log_secondary_agent_error(agent_name: str, error: Exception) -> None:
    logger.error(
        "Agent construction failed while memory preparation was unavailable",
        agent=agent_name,
        error=repr(error),
    )


async def close_unreturned_agent(
    agent: Agent,
    shared_scope_storage: BaseDb | None,
    caller_owned_agent: Agent | None,
) -> None:
    """Close every resource owned by an agent that preparation cannot return."""
    if agent is caller_owned_agent:
        return

    async def close_resources() -> None:
        try:
            await asyncio.to_thread(
                close_agent_runtime_state_dbs,
                agent,
                shared_scope_storage=shared_scope_storage,
            )
        except Exception:
            logger.exception("Failed to close unreturned agent runtime state", agent=agent.id)
        try:
            await aclose_anthropic_async_client(agent.model)
        except Exception:
            logger.exception("Failed to close unreturned agent model client", agent=agent.id)

    await run_coroutine_until_complete(close_resources())


async def _drain_unreturned_agent_build(
    build_future: asyncio.Future[tuple[ResolvedRuntimeModel, Agent]],
    *,
    agent_name: str,
    shared_scope_storage: BaseDb | None,
    caller_owned_agent: Agent | None,
) -> None:
    """Wait through repeated cancellation and clean an unreturned agent build."""
    while not build_future.done():
        try:
            await asyncio.shield(build_future)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    try:
        _, unreturned_agent = build_future.result()
    except asyncio.CancelledError:
        return
    except Exception as error:
        _log_secondary_agent_error(agent_name, error)
    else:
        await close_unreturned_agent(unreturned_agent, shared_scope_storage, caller_owned_agent)


async def _discard_unreturned_agent_result(
    result: tuple[ResolvedRuntimeModel, Agent] | Exception,
    *,
    agent_name: str,
    shared_scope_storage: BaseDb | None,
    caller_owned_agent: Agent | None,
) -> None:
    """Log a failed build or close an agent that preparation cannot return."""
    if isinstance(result, Exception):
        _log_secondary_agent_error(agent_name, result)
    else:
        await close_unreturned_agent(result[1], shared_scope_storage, caller_owned_agent)


async def build_agent_off_loop(
    build_agent: Callable[[], tuple[ResolvedRuntimeModel, Agent]],
    *,
    agent_name: str,
    shared_scope_storage: BaseDb | None,
    caller_owned_agent: Agent | None = None,
) -> tuple[ResolvedRuntimeModel, Agent]:
    """Build one agent off-loop and reclaim a completed result after cancellation."""
    context = contextvars.copy_context()
    build_future = asyncio.get_running_loop().run_in_executor(None, context.run, build_agent)
    try:
        return await asyncio.shield(build_future)
    except asyncio.CancelledError:
        await _drain_unreturned_agent_build(
            build_future,
            agent_name=agent_name,
            shared_scope_storage=shared_scope_storage,
            caller_owned_agent=caller_owned_agent,
        )
        raise


async def prepare_prompt_branches(
    *,
    prepare_memory: Callable[[], Awaitable[MemoryPromptParts]],
    build_agent: Callable[[], tuple[ResolvedRuntimeModel, Agent]],
    agent_name: str,
    shared_scope_storage: BaseDb | None,
    pipeline_timing: DispatchPipelineTiming | None,
    caller_owned_agent: Agent | None = None,
) -> tuple[MemoryPromptParts, ResolvedRuntimeModel, Agent]:
    """Overlap memory preparation with agent construction and join both safely."""

    async def _memory_branch() -> MemoryPromptParts | Exception:
        _mark_pipeline_timing(pipeline_timing, "memory_prepare_start")
        try:
            return await prepare_memory()
        except Exception as error:
            return error
        finally:
            _mark_pipeline_timing(pipeline_timing, "memory_prepare_ready")

    context = contextvars.copy_context()
    _mark_pipeline_timing(pipeline_timing, "agent_build_start")
    build_future = asyncio.get_running_loop().run_in_executor(None, context.run, build_agent)
    build_future.add_done_callback(
        lambda _future: _mark_pipeline_timing(pipeline_timing, "agent_build_ready"),
    )

    async def _agent_branch() -> tuple[ResolvedRuntimeModel, Agent] | Exception:
        try:
            return await asyncio.shield(build_future)
        except Exception as error:
            return error

    try:
        async with asyncio.TaskGroup() as task_group:
            agent_task = task_group.create_task(
                _agent_branch(),
                name=f"agent_prepare:{agent_name}",
            )
            memory_task = task_group.create_task(
                _memory_branch(),
                name=f"memory_prepare:{agent_name}",
            )
    except BaseException:
        await _drain_unreturned_agent_build(
            build_future,
            agent_name=agent_name,
            shared_scope_storage=shared_scope_storage,
            caller_owned_agent=caller_owned_agent,
        )
        raise

    agent_result = agent_task.result()
    try:
        memory_result = memory_task.result()
    except BaseException:
        await _discard_unreturned_agent_result(
            agent_result,
            agent_name=agent_name,
            shared_scope_storage=shared_scope_storage,
            caller_owned_agent=caller_owned_agent,
        )
        raise
    if isinstance(memory_result, Exception):
        await _discard_unreturned_agent_result(
            agent_result,
            agent_name=agent_name,
            shared_scope_storage=shared_scope_storage,
            caller_owned_agent=caller_owned_agent,
        )
        raise memory_result
    if isinstance(agent_result, Exception):
        raise agent_result
    runtime_model, agent = agent_result
    return memory_result, runtime_model, agent
