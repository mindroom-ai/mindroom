"""Concurrent pre-model preparation for agent turns."""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING

from mindroom.history.runtime import close_agent_runtime_state_dbs
from mindroom.logging_config import get_logger
from mindroom.response_shutdown_diagnostics import ResponseShutdownPhase, response_shutdown_phase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.agent import Agent
    from agno.db.base import BaseDb

    from mindroom.config.main import ResolvedRuntimeModel
    from mindroom.memory import MemoryPromptParts
    from mindroom.timing import DispatchPipelineTiming

# Keep extraction behavior-neutral for per-logger routing and emitted logger fields.
logger = get_logger("mindroom.ai")

# Agent construction is synchronous and must stay off the event loop, but the
# default executor is shared with hundreds of unrelated offloads.  Keeping the
# raw concurrent future lets shutdown atomically cancel a build that never
# acquired a worker while still joining and closing one that already started.
_AGENT_BUILD_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="mindroom_agent_build",
)


def _mark_pipeline_timing(pipeline_timing: DispatchPipelineTiming | None, label: str) -> None:
    if pipeline_timing is not None:
        pipeline_timing.mark(label)


def _log_secondary_agent_error(agent_name: str, error: Exception) -> None:
    logger.error(
        "Agent construction failed while memory preparation was unavailable",
        agent=agent_name,
        error=repr(error),
    )


def _close_unreturned_agent(
    agent: Agent,
    shared_scope_storage: BaseDb | None,
    caller_owned_agent: Agent | None,
) -> None:
    if agent is caller_owned_agent:
        return
    try:
        close_agent_runtime_state_dbs(agent, shared_scope_storage=shared_scope_storage)
    except Exception:
        logger.exception("Failed to close unreturned agent runtime state", agent=agent.id)


def _run_agent_build_in_context(
    context: contextvars.Context,
    build_agent: Callable[[], tuple[ResolvedRuntimeModel, Agent]],
) -> tuple[ResolvedRuntimeModel, Agent]:
    """Run one synchronous agent build inside its captured dispatch context."""
    return context.run(build_agent)


async def _drain_unreturned_agent_build(
    build_future: asyncio.Future[tuple[ResolvedRuntimeModel, Agent]],
    build_call: Future[tuple[ResolvedRuntimeModel, Agent]],
    *,
    agent_name: str,
    shared_scope_storage: BaseDb | None,
    caller_owned_agent: Agent | None,
) -> None:
    """Wait through repeated cancellation and clean an unreturned agent build."""
    cancelled_before_start = build_call.cancel()
    logger.info(
        "pre_model_agent_build_cancelled",
        agent=agent_name,
        cancelled_before_start=cancelled_before_start,
    )
    if cancelled_before_start:
        await asyncio.gather(build_future, return_exceptions=True)
        return
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
        _close_unreturned_agent(unreturned_agent, shared_scope_storage, caller_owned_agent)


def _discard_unreturned_agent_result(
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
        _close_unreturned_agent(result[1], shared_scope_storage, caller_owned_agent)


async def _prepare_prompt_branches(
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
    build_call: Future[tuple[ResolvedRuntimeModel, Agent]] = _AGENT_BUILD_EXECUTOR.submit(
        _run_agent_build_in_context,
        context,
        build_agent,
    )
    build_future: asyncio.Future[tuple[ResolvedRuntimeModel, Agent]] = asyncio.wrap_future(build_call)
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
            build_call,
            agent_name=agent_name,
            shared_scope_storage=shared_scope_storage,
            caller_owned_agent=caller_owned_agent,
        )
        raise

    agent_result = agent_task.result()
    try:
        memory_result = memory_task.result()
    except BaseException:
        _discard_unreturned_agent_result(
            agent_result,
            agent_name=agent_name,
            shared_scope_storage=shared_scope_storage,
            caller_owned_agent=caller_owned_agent,
        )
        raise
    if isinstance(memory_result, Exception):
        _discard_unreturned_agent_result(
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


async def prepare_prompt_branches(
    *,
    prepare_memory: Callable[[], Awaitable[MemoryPromptParts]],
    build_agent: Callable[[], tuple[ResolvedRuntimeModel, Agent]],
    agent_name: str,
    shared_scope_storage: BaseDb | None,
    pipeline_timing: DispatchPipelineTiming | None,
    caller_owned_agent: Agent | None = None,
) -> tuple[MemoryPromptParts, ResolvedRuntimeModel, Agent]:
    """Overlap memory preparation with traced, cancellation-safe construction."""
    with response_shutdown_phase(ResponseShutdownPhase.AGENT_PREPARATION):
        return await _prepare_prompt_branches(
            prepare_memory=prepare_memory,
            build_agent=build_agent,
            agent_name=agent_name,
            shared_scope_storage=shared_scope_storage,
            pipeline_timing=pipeline_timing,
            caller_owned_agent=caller_owned_agent,
        )
