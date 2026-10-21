"""Shared private Agno tool preparation and temporary instruction-state workarounds.

Keep SDK internals here; prompt_tokens owns caching and token estimation.
Verified against the pinned Agno version by prompt-surface integration tests.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.agent._tools import determine_tools_for_model
from agno.team._tools import _determine_tools_for_model
from agno.tools.function import Function, FunctionCall
from pydantic import PrivateAttr

from mindroom.tool_system.context_bound_streams import close_async_stream

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agno.agent import Agent
    from agno.run import RunContext
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession
    from agno.team import Team
    from agno.tools.function import FunctionExecutionResult
    from agno.tools.toolkit import Toolkit


# AGNO_COMPAT: Prompt inspection requires private tool preparation.
# Reason: Prompt estimation needs private tool preparation and temporary _tool_instructions.
# Upstream issue: https://github.com/agno-agi/agno/issues/7806
# Upstream PR: https://github.com/agno-agi/agno/pull/7807
# Remove when: Public prepared-request inspection supplies the actual messages, tools,
# and tool instructions without persisting a run or mutating live instruction state.
# Coverage: tests/test_agno_compat_prompt.py::test_prompt_builder_failure_restores_original_instruction_list;
# tests/test_history_prompt_tokens.py.


@contextmanager
def temporary_tool_instructions(entity: Agent | Team, instructions: Sequence[str]) -> Iterator[None]:
    """Restore the exact original instruction list, including when prompt building fails."""
    previous = entity._tool_instructions
    entity._tool_instructions = list(instructions)
    try:
        yield
    finally:
        entity._tool_instructions = previous


@dataclass(frozen=True)
class _PreparedTeamPromptTools:
    """SDK-prepared tools and instructions before restoring the team's live state."""

    tools: tuple[Function | dict, ...]
    tool_instructions: tuple[str, ...]


def prepare_team_prompt_tools(
    team: Team,
    *,
    session: TeamSession,
    run_response: TeamRunOutput,
    run_context: RunContext,
) -> _PreparedTeamPromptTools:
    """Use Agno's actual tool preparation without retaining its instruction mutation."""
    model = team.model
    assert model is not None
    with temporary_tool_instructions(team, team._tool_instructions or ()):
        tools = _determine_tools_for_model(
            team=team,
            model=model,
            run_response=run_response,
            run_context=run_context,
            team_run_context={},
            session=session,
            check_mcp_tools=False,
        )
        return _PreparedTeamPromptTools(tuple(tools), tuple(team._tool_instructions or ()))


# AGNO_COMPAT: Executable tool preparation lacks public run-context and media bindings.
# Reason: RTC and live-turn calls need prepared Functions with Agno's run context and media bindings,
# which Agent.aget_tools alone does not supply through a public preparation API.
# Upstream issue: https://github.com/agno-agi/agno/issues/7806
# Upstream PR: https://github.com/agno-agi/agno/pull/7807 is related inspection work;
# it must also support executable run-context/media bindings to replace this path.
# Remove when: A public Agent preparation API returns the same effective Functions
# for execution; channel filtering and requester authorization remain with the owner.
# Coverage: tests/test_matrix_rtc_call_tools.py::test_build_call_tools_returns_same_agent_prompt_and_tools;
# tests/test_matrix_rtc_call_tools.py::test_build_call_tools_includes_async_only_toolkit_functions;
# tests/test_agent_cli_turn.py::test_real_cli_call_wait_completes_inside_outer_bash.
def prepare_agent_tools(
    agent: Agent,
    *,
    processed_tools: list[Toolkit | Callable | Function | dict],
    run_response: RunOutput,
    run_context: RunContext,
    session: AgentSession,
) -> list[Function | dict]:
    """Prepare executable async Agent tools with Agno's canonical context bindings."""
    assert agent.model is not None
    return determine_tools_for_model(
        agent,
        model=agent.model,
        processed_tools=processed_tools,
        run_response=run_response,
        run_context=run_context,
        session=session,
        async_mode=True,
    )


# AGNO_COMPAT: Model generator consumers and synchronous leaf threads outlive their call owner.
# Reason: Agno 3.0.9 arun_function_calls starts generator_tasks without cancellation
# or joining on iterator close; deferred pulls can execute after owner cleanup.
# Unhooked synchronous calls also use to_thread(execute) without retaining completion.
# Upstream issue: Tracking gap; verified in installed Agno and offline cancellation
# regression. No upstream issue has been identified for this lifecycle gap.
# Upstream PR: None identified.
# Remove when: Model.arun_function_calls owns and closes all result iterators and
# cancels/joins every generator consumer and settles synchronous threads before
# returning or propagating cancellation.
# Coverage: tests/test_agent_tool_calls.py::test_closing_stream_stops_async_generator_before_catalog_release;
# tests/test_agent_tool_calls.py::test_cancelled_sync_leaf_keeps_resources_until_thread_finishes.
class OwnedAgentFunctionCall(FunctionCall):
    """Normal Agno call with explicit ownership of result-stream consumers."""

    _owner_closed: bool = PrivateAttr(default=False)
    _owner_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _sync_started: bool = PrivateAttr(default=False)
    _sync_done: Future[None] = PrivateAttr(default_factory=Future)
    _consumers: set[asyncio.Task] = PrivateAttr(default_factory=set)
    _async_result: AsyncIterator | None = PrivateAttr(default=None)
    _sync_result: Iterator | None = PrivateAttr(default=None)

    async def aexecute(self) -> FunctionExecutionResult:
        """Keep Agno execution and hooks intact, then own returned iterators."""
        result = await super().aexecute()
        self._own_result()
        return result

    def execute(self) -> FunctionExecutionResult:
        """Fence and track Agno's unhooked synchronous to_thread execution path."""
        with self._owner_lock:
            if self._owner_closed:
                raise asyncio.CancelledError
            self._sync_started = True
        try:
            result = super().execute()
            self._own_result()
            return result
        finally:
            self._sync_done.set_result(None)

    def _own_result(self) -> None:
        if isinstance(self.result, AsyncIterator):
            self._async_result = self.result
            self.result = self._consume_async(self.result)
        elif isinstance(self.result, Iterator):
            self._sync_result = self.result
            self.result = self._consume_sync(self.result)

    async def _consume_async(self, source: AsyncIterator) -> AsyncIterator:
        if self._owner_closed:
            return
        task = asyncio.current_task()
        assert task is not None
        self._consumers.add(task)
        try:
            async for item in source:
                if self._owner_closed:
                    return
                yield item
        finally:
            self._async_result = None
            try:
                await close_async_stream(source)
            finally:
                self._consumers.discard(task)

    def _consume_sync(self, source: Iterator) -> Iterator:
        if self._owner_closed:
            return
        try:
            for item in source:
                if self._owner_closed:
                    return
                yield item
        finally:
            self._sync_result = None
            close = getattr(source, "close", None)
            if close is not None:
                close()

    async def close_result(self) -> None:
        """Fence future consumers and settle active consumers before owner release."""
        with self._owner_lock:
            self._owner_closed = True
            sync_started = self._sync_started
        if sync_started:
            await asyncio.wrap_future(self._sync_done)
        tasks = tuple(self._consumers)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async_result, self._async_result = self._async_result, None
        await close_async_stream(async_result)
        sync_result, self._sync_result = self._sync_result, None
        close = getattr(sync_result, "close", None)
        if close is not None:
            close()


# AGNO_COMPAT: A nested control tool must stop its owning provider call.
# Reason: Agno builds ToolExecution.stop_after_tool_call from Function after the
# entrypoint returns; restoring the shared Function early loses the stop signal.
# Upstream issue: Tracking gap; nested hidden calls have no public stop API.
# Upstream PR: None identified.
# Remove when: FunctionCall exposes a public per-execution stop signal.
# Coverage: tests/test_agent_cli_control_tools.py::test_control_stops_batch_and_settles_admitted_work.
def stop_function_call(call: FunctionCall) -> None:
    """Keep an execution-local Function until Agno has constructed its result."""
    if call.function is not None:
        call.function = call.function.model_copy(deep=False)
        call.function.stop_after_tool_call = True


# AGNO_COMPAT: Tool preparation does not expose the per-run Function it creates.
# Reason: Agno prepare_tools makes a fresh per-run Function copy, and the CLI checkpoint
# must recognize that exact provider Function in each captured batch.
# Upstream issue: Tracking gap; no public API reports prepared per-run Functions.
# Upstream PR: None identified.
# Remove when: Public preparation exposes the bound per-run Function for each tool.
# Coverage: tests/test_minimal_agent.py::test_prepared_bash_facade_reports_its_per_run_copy.
class BashPresentationFunction(Function):
    """An exact Bash facade exception; canonical Functions retain normal preparation."""

    _on_prepare: Callable[[Function], None] | None = PrivateAttr(default=None)

    def bind_preparation(self, callback: Callable[[Function], None] | None) -> None:
        """Register the actual copied provider Function with its existing owner."""
        self._on_prepare = callback

    def _per_run_copy(self) -> Function:
        copied = super()._per_run_copy()
        assert isinstance(copied, BashPresentationFunction)
        copied._on_prepare = self._on_prepare
        if copied._on_prepare is not None:
            copied._on_prepare(copied)
        return copied
