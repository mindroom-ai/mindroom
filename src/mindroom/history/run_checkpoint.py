"""Checkpoints of a running run, and the recovery of a run that ended without finishing.

A run stores its terminal record once, when it ends. Its hooks also save it at every tool-batch boundary as a
cancelled row, which history never replays, so a process that dies keeps the run's work through the last boundary.
Recovery turns such a row, or the cancelled row a stop or an orderly shutdown left, into a finished turn that holds the
stored messages as the run sent them. Later requests replay them unchanged, so they extend the interrupted run's
requests and a provider's prompt cache still matches them.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, NamedTuple

from mindroom.agent_loop.model_loop import SendRequest
from mindroom.agent_loop.tools import tool_result_message
from mindroom.constants import CONTINUED_RUNS_METADATA_KEY, MATRIX_RESPONSE_EVENT_ID_METADATA_KEY
from mindroom.history.run_visibility import is_model_history_visible_run, run_in_scope
from mindroom.llm.runs import RunStatus, ToolExecution
from mindroom.logging_config import get_logger
from mindroom.session_storage.store import source_event_ids, write_with_session_state

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from mindroom.agent_loop.model_loop import BeforeDispatchHook, BeforeRequestHook, LoopState, RequestDecision
    from mindroom.agent_loop.tools import PlannedCall
    from mindroom.history.types import HistoryScope
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.run_storage import RunStorage
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession

logger = get_logger(__name__)

# What answers a call that had no result when its run was cut short, so every stored call has an answer.
INTERRUPTED_CALL_RESULT = (
    "Interrupted before this call returned a result, so it may have partly run. "
    "Check whether it took effect before repeating a call with side effects."
)
# Set in the metadata of a run recovery finished, which did not end on its own.
_RECOVERED_RUN_KEY = "mindroom_recovered_run"
_CRASH_LOOP_ERROR = "Left out of history: the turn kept being interrupted while it continued from this run."


class CheckpointHooks(NamedTuple):
    """The two loop hooks that checkpoint a run: after each tool batch's results, and before each batch runs."""

    before_request: BeforeRequestHook
    before_dispatch: BeforeDispatchHook


async def save_run_checkpoint(store: ConversationStore, session: ConversationSession, run: AnyRunRecord) -> None:
    """Save ``run``, a copy of a running run nothing else changes, as a cancelled row; storage failures propagate.

    The row is cancelled so that, if the process dies before the run's terminal record replaces it, history skips it
    until recovery finishes it. The run and the session state its tools ran with commit together, without its usage.
    """
    run.status = RunStatus.cancelled
    await write_with_session_state(
        store,
        session,
        lambda opened, state: opened.save_run(session, run, record_usage=False, state=state),
    )


def checkpoint_hooks(storage: RunStorage | None) -> CheckpointHooks | None:
    """Return the hooks that checkpoint a run stored in ``storage``, or None for a run that stores no turn.

    The hooks save the run before a tool batch runs, so its row ends in the calls about to take effect, and before
    each request of a run that has called a tool, so it holds the batch's results, a subagent's answer the run paused
    for included. A checkpoint a reply can do without: a failed save is logged.
    """
    if storage is None or storage.store is None or storage.usage_kind is not None:
        return None
    store, session = storage.store, storage.session

    async def checkpoint(state: LoopState) -> None:
        # The run's task waits here, so nothing changes the live record and messages the copy shares.
        run = replace(state.record, messages=list(state.messages))
        try:
            await save_run_checkpoint(store, session, run)
        except Exception:
            logger.warning("run_checkpoint_failed", run_id=run.run_id, exc_info=True)

    async def before_request(state: LoopState) -> RequestDecision:
        # A run that has called no tool holds only its prompt, which a replayed turn sends again.
        if state.record.tools:
            await checkpoint(state)
        return SendRequest()

    async def before_dispatch(state: LoopState, _planned: Sequence[PlannedCall]) -> None:
        await checkpoint(state)

    return CheckpointHooks(before_request, before_dispatch)


def is_recovered_run(run: AnyRunRecord) -> bool:
    """Return whether recovery finished ``run``, which was cut short and did not complete on its own."""
    return bool((run.metadata or {}).get(_RECOVERED_RUN_KEY))


def recover_interrupted_run(store: ConversationStore, session: ConversationSession, run: AnyRunRecord) -> bool:
    """Finish the cancelled top-level ``run`` of ``session`` as a turn history replays; return whether it is one now.

    The turn keeps the run's stored messages exactly, reasoning included, so a later request replays what the run's
    own requests sent. Only what no request could follow changes: the usage a cut-off response left on a message
    without content goes, and each call of the last batch that has no result message gets one, the result its
    execution recorded when a sibling finished before the stop, else ``INTERRUPTED_CALL_RESULT``. A run that called
    no tool holds no work to keep and stays as it is. The run's usage is recorded, which a checkpoint never is.
    """
    if run.status is not RunStatus.cancelled or run.parent_run_id is not None:
        return False
    messages = list(run.messages)
    while (
        messages and messages[-1].role == "assistant" and not messages[-1].tool_calls and messages[-1].content is None
    ):
        messages.pop()
    calling = max((index for index, message in enumerate(messages) if message.tool_calls), default=None)
    if calling is None:
        return False
    # The batch's results follow its assistant message; a batch cut short stored none of them.
    end = calling + 1
    while end < len(messages) and messages[end].role == "tool":
        end += 1
    answered = {message.tool_call_id for message in messages[calling + 1 : end]}
    tools = list(run.tools or ())
    # A provider may reuse a call id within one run; the last batch's executions are the last recorded.
    places = {tool.tool_call_id: place for place, tool in enumerate(tools)}
    results = []
    for call in messages[calling].tool_calls or ():
        call_id = call["id"]
        if call_id in answered:
            continue
        if call_id not in places:
            places[call_id] = len(tools)
            tools.append(ToolExecution(tool_call_id=call_id, tool_name=call["function"]["name"]))
        execution = tools[places[call_id]]
        if execution.result is None and execution.is_error is None:
            execution = tools[places[call_id]] = replace(
                execution,
                result=INTERRUPTED_CALL_RESULT,
                is_error=True,
                requires_confirmation=None,
                external_execution_required=None,
            )
        results.append(tool_result_message(execution))
    recovered = replace(
        run,
        messages=[*messages[:end], *results, *messages[end:]],
        tools=tools,
        status=RunStatus.completed,
        error=None,
        metadata={**(run.metadata or {}), _RECOVERED_RUN_KEY: True},
    )
    return store.save_run(session, recovered, record_usage=True) == "written"


def recover_interrupted_turn(
    store: ConversationStore,
    session_id: str,
    scope: HistoryScope,
    event_ids: Collection[str],
    *,
    natively: bool,
) -> list[str]:
    """Recover the runs of ``scope`` cut short while they answered ``event_ids``; return the recovered runs it holds.

    A turn a restart interrupted is replayed, and its replay calls this before it loads history, so its request
    continues from the interrupted attempt's own messages. Without ``natively``, for a turn that keeps being
    interrupted, nothing is recovered and the runs recovered for it earlier leave history as failed runs, so its next
    attempt starts from the conversation before the turn. A recovered run a compaction folded stays in the scope's
    summary either way and is returned, so the turn's answer links it too (``link_response_event``).
    """
    session = store.load_session(session_id)
    if session is None:
        return []
    loaded = {run.run_id for run in session.runs}
    recovered: list[str] = []
    # The loaded session leaves out the runs a summary covers, so the turn's attempts are read from every stored run.
    for run in store.list_runs(session_id):
        if run.parent_run_id is not None or not run_in_scope(run, scope):
            continue
        if not source_event_ids(run.metadata).intersection(event_ids):
            continue
        earlier = is_recovered_run(run) and is_model_history_visible_run(run)
        if natively and (earlier or recover_interrupted_run(store, session, run)):
            recovered.append(run.run_id)
        elif earlier and run.run_id in loaded:
            store.save_interrupted_record(session, replace(run, status=RunStatus.error, error=_CRASH_LOOP_ERROR))
        elif earlier:
            recovered.append(run.run_id)
    return recovered


def link_response_event(store: ConversationStore, session_id: str, run_id: str, response_event_id: str) -> None:
    """Link run ``run_id`` to the Matrix message that shows its answer, and the recovered runs it continued with it.

    The run's metadata names those runs (``CONTINUED_RUNS_METADATA_KEY``). Redacting the message then removes the
    interrupted attempts together with the answer that finished their turn.
    """
    link = {MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: response_event_id}
    store.patch_run_metadata(session_id, run_id, link)
    run = store.get_run(run_id)
    for continued in (run.metadata or {}).get(CONTINUED_RUNS_METADATA_KEY, ()) if run is not None else ():
        store.patch_run_metadata(session_id, continued, link)


__all__ = [
    "INTERRUPTED_CALL_RESULT",
    "CheckpointHooks",
    "checkpoint_hooks",
    "is_recovered_run",
    "link_response_event",
    "recover_interrupted_run",
    "recover_interrupted_turn",
    "save_run_checkpoint",
]
