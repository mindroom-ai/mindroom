"""Attempt-scoped capture of the real provider batch before CLI effects."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from types import MethodType
from typing import TYPE_CHECKING, Any

from agno.db.base import BaseDb
from agno.models.response import ToolExecution
from agno.run.base import RunStatus
from agno.tools.function import FunctionExecutionResult, ToolResult

from mindroom.agent_storage import run_session_storage_operation, save_runs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agno.agent import Agent
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.session.agent import AgentSession
    from agno.tools.function import Function, FunctionCall

    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog


@dataclass
class _Batch:
    messages: list[Message]
    model: Model
    persisted: bool = False


# AGNO_COMPAT: Agno populates RunOutput.messages only after the model loop.
# Reason: CLI effects inside Bash require the actual provider batch durably saved
# before the public after-tool-results checkpoint can execute. asave_session saves
# metadata only and Agno storage wrappers swallow errors; use owned storage below.
# Upstream issue: Tracking gap; verified against pinned Agno 3.0.9, and no issue requests
# a pre-dispatch callback carrying provider history.
# Upstream PR: None identified.
# Remove when: Public pre-tool-batch callback provides immutable real history and
# propagating session/run persistence before any tool is dispatched.
# Coverage: tests/test_agent_cli_checkpoint.py::test_real_fallback_batch_checkpoints_resolved_model;
# tests/test_agent_cli_checkpoint.py::test_lifetime_capture_resolves_owner_created_during_first_pull;
# tests/test_agent_cli_checkpoint.py::test_checkpoint_retains_only_the_current_provider_batch.
_INNER: ContextVar[bool] = ContextVar("cli_inner_dispatch", default=False)
_RESOLVER: ContextVar[Callable[[], tuple[ProviderBatchCheckpoint, Function] | None] | None] = ContextVar(
    "cli_checkpoint_resolver",
    default=None,
)


@contextmanager
def checkpoint_resolver(resolve: Callable[[], tuple[ProviderBatchCheckpoint, Function] | None]) -> Iterator[None]:
    """Resolve a lazily prepared provider binding only inside its response context."""
    token = _RESOLVER.set(resolve)
    try:
        yield
    finally:
        _RESOLVER.reset(token)


@contextmanager
def inner_cli_dispatch() -> Iterator[None]:
    """Prevent nested prepared calls from replacing real provider history."""
    token = _INNER.set(True)
    try:
        yield
    finally:
        _INNER.reset(token)


def _install_capture(model: Model) -> None:
    original = model.get_function_calls_to_run
    if getattr(original, "_mindroom_cli_capture", False):
        return
    # A bound MethodType survives Agno's deepcopy during fallback resolution.
    # Retaining the old bound method in a closure would call the previous model.
    original_function = original.__func__

    def observed(
        observed_model: Model,
        assistant_message: Message,
        messages: list[Message],
        functions: dict[str, Function] | None = None,
    ) -> list[FunctionCall]:
        calls = original_function(observed_model, assistant_message, messages, functions)
        resolver = _RESOLVER.get()
        active = resolver() if resolver is not None else None
        if (
            not _INNER.get()
            and active is not None
            and any(observed_model is candidate for candidate in active[0]._models)
            and functions is not None
            and any(value is active[1] for value in functions.values())
        ):
            batch = _Batch(deepcopy(messages), observed_model)
            # A new provider batch means every earlier one finished; keep only this snapshot.
            active[0]._calls.clear()
            for call in calls:
                if call.function is active[1]:
                    active[0]._calls[id(call)] = (call, batch)
        return calls

    observed._mindroom_cli_capture = True  # ty: ignore[unresolved-attribute]
    model.get_function_calls_to_run = MethodType(observed, model)  # ty: ignore[invalid-assignment]


class ProviderBatchCheckpoint:
    """Own snapshots by exact returned FunctionCall identity, never by its name."""

    def __init__(self, catalog: PreparedAgentToolCatalog) -> None:
        self.catalog = catalog
        self._calls: dict[int, tuple[FunctionCall, _Batch]] = {}
        self._lock = asyncio.Lock()
        self._models: tuple[Model, ...] = ()
        self._saved_parent: str | None = None

    def resume_saved_parent(self, parent_call_id: str) -> None:
        """Adopt the already-persisted provider history for one claimed hidden call."""
        matches = [
            call
            for message in self.catalog.run_response.messages or ()
            if message.role == "assistant"
            for call in message.tool_calls or ()
            if call.get("id") == parent_call_id
        ]
        if len(matches) != 1:
            msg = "Recovered approval has no exact saved parent Bash call"
            raise ValueError(msg)
        self._saved_parent = parent_call_id

    def prepare_capture(self) -> None:
        """Install inert exact-model wrappers when real preparation completes."""
        model = self.catalog.agent.model
        assert model is not None
        fallback = self.catalog.agent.fallback_config
        models = [model]
        if fallback is not None:
            for candidate in (*fallback.on_error, *fallback.on_rate_limit, *fallback.on_context_overflow):
                if isinstance(candidate, str):
                    msg = "CLI capture requires resolved fallback models"
                    raise TypeError(msg)
                models.append(candidate)
        self._models = tuple(models)
        for candidate in self._models:
            _install_capture(candidate)

    def clear(self) -> None:
        """Retire captured calls only at the owning attempt boundary."""
        self._calls.clear()

    def complete(self, call: FunctionCall, result: str | ToolResult) -> None:
        """Retain an observed Bash result if a sibling later waits for approval."""
        captured = self._calls.get(id(call))
        if captured is None:
            msg = "Provider Bash result has no captured real batch"
            raise ValueError(msg)
        batch = captured[1]
        execution = (
            FunctionExecutionResult(
                status="success",
                result=result.content,
                images=result.images,
                audios=result.audios,
                videos=result.videos,
                files=result.files,
            )
            if isinstance(result, ToolResult)
            else None
        )
        batch.messages.append(
            batch.model.create_function_call_result(
                call,
                success=True,
                output=result.content if isinstance(result, ToolResult) else result,
                function_execution_result=execution,
            ),
        )

    async def persist_approval(self, parent_call_id: str) -> None:
        """Refresh the real batch and mutable session immediately before approval."""
        if parent_call_id == self._saved_parent:
            async with self._lock:
                await self._save(deepcopy(self.catalog.run_response))
            return
        matches = [call for call, _batch in self._calls.values() if call.call_id == parent_call_id]
        if len(matches) != 1:
            msg = "Approval has no exact captured parent Bash call"
            raise ValueError(msg)
        await self.persist(matches[0], refresh=True)

    async def persist(self, call: FunctionCall, *, refresh: bool = False) -> None:
        """Checkpoint once per captured batch; storage failures propagate unchanged."""
        captured = self._calls.get(id(call))
        if captured is None:
            msg = "Provider Bash call has no captured real batch"
            raise ValueError(msg)
        batch = captured[1]
        async with self._lock:
            if batch.persisted and not refresh:
                return
            run = deepcopy(self.catalog.run_response)
            # The batch snapshot is already private; later results only append to it.
            run.messages = list(batch.messages)
            results = {message.tool_call_id: message for message in batch.messages if message.role == "tool"}
            observed = {}
            for observed_call, _batch in self._calls.values():
                result = results.get(observed_call.call_id)
                observed[observed_call.call_id] = ToolExecution(
                    tool_call_id=observed_call.call_id,
                    tool_name=observed_call.function.name,
                    tool_args=deepcopy(observed_call.arguments),
                    result=str(result.content) if result is not None else None,
                    tool_call_error=result.tool_call_error if result is not None else None,
                )
            run.tools = [tool for tool in run.tools or () if tool.tool_call_id not in observed] + list(
                observed.values(),
            )

            await self._save(run)
            batch.persisted = True

    async def _save(self, run: RunOutput) -> None:
        # The checkpoint ends in an unanswered Bash call. If the process dies before
        # Agno's own final save replaces it, history must skip it, or every later
        # turn in the session would send a tool call without its result.
        run.status = RunStatus.cancelled
        await save_cli_session(
            self.catalog.agent,
            self.catalog.session,
            self.catalog.run_context.session_state,
            lambda database, session: save_runs(database, session, [run]),
        )


async def save_cli_session(
    agent: Agent,
    session: AgentSession,
    session_state: dict[str, Any] | None,
    write_runs: Callable[[BaseDb, AgentSession], None],
) -> None:
    """Upsert a session snapshot with its current state, then its runs, through propagating owned storage."""
    storage = agent.db
    if not isinstance(storage, BaseDb):
        msg = "CLI history requires owned synchronous session storage"
        raise TypeError(msg)
    saved = deepcopy(session)
    saved.session_data = dict(saved.session_data or {})
    saved.session_data["session_state"] = deepcopy(session_state)

    def write(database: BaseDb) -> None:
        database.upsert_session(session=saved)
        write_runs(database, saved)

    await run_session_storage_operation(lambda: storage, write)
