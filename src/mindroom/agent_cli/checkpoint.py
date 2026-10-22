"""Minimal Bash's provider batch, saved before any of its calls takes effect.

The loop hands each tool batch to ``BatchCheckpoint.capture`` before it runs any call, so a Bash call can save the
run with the exact provider history that requested it, ending in that unanswered call, before its shell or any CLI
call inside it does anything. The saved run is marked cancelled: if the process dies before the run's own terminal
record replaces it, history skips it, as every later turn would otherwise send a tool call without its result.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.agent_loop.tools import tool_result_message
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.session_storage.lanes import run_session_storage_operation

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.agent_loop.model_loop import LoopState
    from mindroom.agent_loop.tools import PlannedCall
    from mindroom.llm.messages import Message
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog
    from mindroom.tool_system.agno_toolkits import ToolValue

__all__ = ["BatchCheckpoint", "save_cli_session"]


@dataclass
class _Batch:
    """One provider batch: the history up to its assistant message, its Bash calls, and the results seen so far."""

    messages: list[Message]
    calls: Mapping[str, PlannedCall]
    persisted: bool = False


class BatchCheckpoint:
    """Own the snapshot of the provider batch whose Bash calls are running, keyed by provider call id."""

    def __init__(self, catalog: PreparedAgentToolCatalog) -> None:
        self.catalog = catalog
        self._batch: _Batch | None = None
        self._lock = asyncio.Lock()
        self._saved_parent: str | None = None

    async def capture(self, state: LoopState, planned: Sequence[PlannedCall]) -> None:
        """Snapshot a new batch before any of its calls runs; a new batch means every earlier one finished."""
        record = state.record
        assert isinstance(record, RunRecord)
        self.catalog.record = record
        self._batch = _Batch(
            messages=deepcopy(state.messages),
            calls={call.call_id: call for call in planned if call.call_id},
        )

    def resume_saved_parent(self, parent_call_id: str) -> None:
        """Adopt the already-saved provider history of one recovered Bash call."""
        record = self.catalog.record
        matches = [
            call
            for message in (record.messages if record is not None else ())
            if message.role == "assistant"
            for call in message.tool_calls or ()
            if call.get("id") == parent_call_id
        ]
        if len(matches) != 1:
            msg = "Recovered approval has no exact saved parent Bash call"
            raise ValueError(msg)
        self._saved_parent = parent_call_id

    def _batch_of(self, call_id: str, message: str) -> _Batch:
        if self._batch is None or call_id not in self._batch.calls:
            raise ValueError(message)
        return self._batch

    def complete(self, call_id: str, result: str | ToolValue) -> None:
        """Keep a finished Bash result in its batch, in case a sibling later waits for approval."""
        batch = self._batch_of(call_id, "Provider Bash result has no captured real batch")
        call = batch.calls[call_id]
        media = None if isinstance(result, str) else result
        execution = ToolExecution(
            tool_call_id=call_id,
            tool_name=call.function.name,
            tool_args=deepcopy(call.arguments),
            tool_call_error=False,
            result=result if isinstance(result, str) else result.content,
        )
        batch.messages.append(tool_result_message(execution, media=media))

    async def persist_approval(self, parent_call_id: str) -> None:
        """Refresh the saved batch and session state immediately before an approval wait."""
        if parent_call_id == self._saved_parent:
            async with self._lock:
                record = self.catalog.record
                assert record is not None
                await self._save(deepcopy(record))
            return
        self._batch_of(parent_call_id, "Approval has no exact captured parent Bash call")
        await self.persist(parent_call_id, refresh=True)

    async def persist(self, call_id: str, *, refresh: bool = False) -> None:
        """Save the batch of ``call_id`` once, or again with ``refresh``; storage failures propagate unchanged."""
        batch = self._batch_of(call_id, "Provider Bash call has no captured real batch")
        async with self._lock:
            if batch.persisted and not refresh:
                return
            record = self.catalog.record
            assert record is not None
            run = deepcopy(record)
            # The batch snapshot is already private; later results only append to it.
            run.messages = list(batch.messages)
            results = {message.tool_call_id: message for message in batch.messages if message.role == "tool"}
            observed: dict[str, ToolExecution] = {}
            for observed_id, call in batch.calls.items():
                result = results.get(observed_id)
                observed[observed_id] = ToolExecution(
                    tool_call_id=observed_id,
                    tool_name=call.function.name,
                    tool_args=deepcopy(call.arguments),
                    result=result.get_content_string() if result is not None else None,
                    tool_call_error=result.tool_call_error if result is not None else None,
                )
            run.tools = [tool for tool in run.tools or () if tool.tool_call_id not in observed] + list(
                observed.values(),
            )
            await self._save(run)
            batch.persisted = True

    async def _save(self, run: RunRecord) -> None:
        run.status = RunStatus.cancelled
        catalog = self.catalog
        await save_cli_session(
            catalog.store,
            catalog.session,
            catalog.run.session_state,
            lambda store, session: store.save_run(session, run, record_usage=False),
        )


async def save_cli_session(
    store: ConversationStore | None,
    session: ConversationSession,
    session_state: Mapping[str, object],
    write: Callable[[ConversationStore, ConversationSession], object],
) -> None:
    """Save the session's current state, then ``write``'s runs, in the store's lane; failures propagate."""
    if store is None:
        msg = "CLI history requires persisted session storage"
        raise RuntimeError(msg)
    saved = replace(session, session_state=deepcopy(dict(session_state)))

    def operation(opened: ConversationStore) -> None:
        opened.save_session_fields(saved, "session_state")
        write(opened, saved)

    await run_session_storage_operation(lambda: store, operation)
