"""Internal AI execution helpers kept off the public ``mindroom.ai`` seam."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.history.run_visibility import (
    QUEUED_MESSAGE_NOTICE_MARKER_KEY,
    QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER,
    is_model_history_visible_run,
    is_queued_notice_message,
    queued_notice_marker,
)
from mindroom.llm.messages import Message
from mindroom.logging_config import get_logger
from mindroom.session_storage.lanes import run_session_storage_operation

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from mindroom.agent_loop.model_loop import AfterToolBatchHook
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.judgment.state import JudgmentMessage
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.mid_turn import MidTurnGate, QueuedMessage
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import SessionKind

__all__ = [
    "EMPTY_RESPONSE_NOTICE",
    "bind_mid_turn_conversation_context",
    "finalize_queued_notice_response_turn_async",
    "queued_message_signal_context",
    "queued_notice_after_tool_batch",
    "queued_notice_on_resume_entry",
    "register_scope_notice_storage",
    "scrub_queued_notice_session_context",
]

logger = get_logger(__name__)


_QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY = "mindroom_queued_message_notice_response_turn_id"

EMPTY_RESPONSE_NOTICE = "The model returned an empty response — please try again."


class _SupportsQueuedMessageState(Protocol):
    def has_pending_human_messages(self) -> bool: ...

    def pending_message_snapshot(self) -> tuple[QueuedMessage, ...]: ...


@dataclass
class _QueuedMessageNoticeContext:
    state: _SupportsQueuedMessageState | None
    mid_turn_gate: MidTurnGate | None = None
    response_turn_id: str = field(default_factory=lambda: str(uuid4()))
    notice_fired: bool = False
    storage_targets: dict[tuple[str, str, SessionKind], _QueuedNoticeStorageTarget] = field(default_factory=dict)


@dataclass
class _QueuedNoticeStorageTarget:
    storage_factory: Callable[[], ConversationStore]
    session_id: str
    session_kind: SessionKind
    entity_name: str


_queued_message_notice_context: ContextVar[_QueuedMessageNoticeContext | None] = ContextVar(
    "queued_message_notice_context",
    default=None,
)


@contextmanager
def queued_message_signal_context(
    signal: _SupportsQueuedMessageState | None,
    *,
    mid_turn_gate: MidTurnGate | None = None,
) -> Generator[_QueuedMessageNoticeContext, None, None]:
    """Bind one queued-message signal to the current async task."""
    notice_context = _QueuedMessageNoticeContext(state=signal, mid_turn_gate=mid_turn_gate)
    token = _queued_message_notice_context.set(notice_context)
    try:
        yield notice_context
    finally:
        _queued_message_notice_context.reset(token)


def bind_mid_turn_conversation_context(
    context_factory: Callable[[], tuple[JudgmentMessage, ...] | None],
) -> None:
    """Build refreshed public context only when this task has an active judge."""
    notice = _queued_message_notice_context.get()
    if notice is not None and notice.mid_turn_gate is not None:
        notice.mid_turn_gate.bind_conversation_context(context_factory())


def _queued_notice_response_turn_id(message: Message) -> str | None:
    provider_data = message.provider_data
    if not isinstance(provider_data, dict):
        return None
    response_turn_id = provider_data.get(_QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY)
    return response_turn_id if isinstance(response_turn_id, str) and response_turn_id else None


def _is_queued_notice_message(
    message: Message,
    *,
    response_turn_id: str | None = None,
) -> bool:
    """Return whether one message is the hidden queued-message notice."""
    if not is_queued_notice_message(message):
        return False
    if response_turn_id is None:
        return True
    return _queued_notice_response_turn_id(message) == response_turn_id


def _strip_queued_notice_messages(messages: list[Message], *, response_turn_id: str) -> None:
    """Remove one response's queued-message notices from a mutable message list."""
    messages[:] = [
        message for message in messages if not _is_queued_notice_message(message, response_turn_id=response_turn_id)
    ]


def _append_queued_notice_if_needed(
    *,
    messages: list[Message],
    notice_text: str,
    judged: bool = False,
) -> None:
    notice_context = _queued_message_notice_context.get()
    if notice_context is None or (notice_context.mid_turn_gate is not None and not judged):
        return
    _strip_queued_notice_messages(messages, response_turn_id=notice_context.response_turn_id)
    if notice_context.state is None or not notice_context.state.has_pending_human_messages():
        return
    messages.append(
        Message(
            role="user",
            content=notice_text,
            provider_data={
                QUEUED_MESSAGE_NOTICE_MARKER_KEY: True,
                _QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY: notice_context.response_turn_id,
            },
        ),
    )
    if not notice_context.notice_fired:
        notice_context.notice_fired = True
        logger.info(
            "queued_message_notice_injected",
            response_turn_id=notice_context.response_turn_id,
        )


def _ends_with_completed_tool_batch(
    messages: list[Message],
    response_turn_id: str,
    *,
    after_tool_batch: bool = False,
) -> bool:
    """Return whether the messages end with a complete tool batch, found without crossing a real user message."""
    results: list[Message] = []
    for message in reversed(messages):
        if _is_queued_notice_message(message, response_turn_id=response_turn_id):
            continue
        # Formatting may append user-role media after a just-completed batch, and a paused call's
        # result follows that media when the run resumes. Only a trailing user message is real input,
        # so at response entry it still stops this scan.
        if message.role == "user" and (after_tool_batch or results):
            continue
        if message.role == "tool":
            results.append(message)
            continue
        if message.role == "assistant" and message.tool_calls:
            call_ids = {call.get("id") for call in message.tool_calls}
            result_ids = {result.tool_call_id for result in results}
            return None not in call_ids and call_ids <= result_ids
        return False
    return False


async def _judge_queued_notice(messages: list[Message], *, notice_text: str) -> None:
    context = _queued_message_notice_context.get()
    if context is None or context.mid_turn_gate is None or context.state is None:
        return
    state = context.state
    pending = state.pending_message_snapshot()
    if not pending:
        return
    if not context.notice_fired and await context.mid_turn_gate.defers(
        pending,
        current=lambda: not context.notice_fired and pending == state.pending_message_snapshot(),
    ):
        return
    _append_queued_notice_if_needed(messages=messages, notice_text=notice_text, judged=True)


def queued_notice_after_tool_batch(notice_text: str) -> AfterToolBatchHook:
    """Return the loop hook that appends the queued-message notice after a tool batch when a newer message waits.

    Without a mid-turn judge the notice follows every batch while messages are queued; with one, the judge decides
    after each completed batch. Calls paused for approval or delegation leave the batch unresolved; the response that
    resumes it judges the completed batch, reusing this turn's decision for an unchanged queue.
    """

    async def after_tool_batch(messages: list[Message], _results: Sequence[Message]) -> None:
        _append_queued_notice_if_needed(messages=messages, notice_text=notice_text)
        context = _queued_message_notice_context.get()
        if context is None or context.mid_turn_gate is None:
            return
        if not _ends_with_completed_tool_batch(messages, context.response_turn_id, after_tool_batch=True):
            return
        await _judge_queued_notice(messages, notice_text=notice_text)

    return after_tool_batch


def queued_notice_on_resume_entry(notice_text: str) -> AfterToolBatchHook:
    """Return the loop hook that notifies a resumed run of a queued message after its completed tool batch."""

    async def on_resume_entry(messages: list[Message], _results: Sequence[Message]) -> None:
        context = _queued_message_notice_context.get()
        if context is None or not _ends_with_completed_tool_batch(messages, context.response_turn_id):
            return
        if context.mid_turn_gate is not None:
            await _judge_queued_notice(messages, notice_text=notice_text)
        elif context.state is not None and context.state.has_pending_human_messages():
            _append_queued_notice_if_needed(messages=messages, notice_text=notice_text)

    return on_resume_entry


def _run_notice_messages(run: AnyRunRecord, *, response_turn_id: str | None) -> list[Message]:
    """Return the notices of one response, or of any response when ``response_turn_id`` is None, in a run.

    Only a top-level run holds notices: team members run without them.
    """
    return [
        message for message in run.messages if _is_queued_notice_message(message, response_turn_id=response_turn_id)
    ]


def _new_persisted_queued_notice(response_turn_id: str, notice_text: str) -> Message:
    return Message(
        role="user",
        content=notice_text,
        provider_data={
            QUEUED_MESSAGE_NOTICE_MARKER_KEY: QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER,
            _QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY: response_turn_id,
        },
    )


def _queued_notice_text_to_persist(
    *,
    destination_matches: Sequence[Message],
) -> str | None:
    for message in destination_matches:
        if queued_notice_marker(message) is True and isinstance(message.content, str):
            return message.content
    for message in destination_matches:
        if queued_notice_marker(message) == QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER and isinstance(message.content, str):
            return message.content
    return None


def _finalize_queued_notice_in_runs(
    runs: Sequence[AnyRunRecord],
    *,
    response_turn_id: str,
) -> list[AnyRunRecord]:
    """Leave one exact persisted notice where the newest replayable run saw it.

    Returns copies of the runs that change; the given runs are left untouched
    because loaded run objects are shared across readers of a session.
    """
    destination = next(
        (
            run
            for run in reversed(runs)
            if is_model_history_visible_run(run) and _run_notice_messages(run, response_turn_id=response_turn_id)
        ),
        None,
    )
    all_matches = [message for run in runs for message in _run_notice_messages(run, response_turn_id=response_turn_id)]
    if not all_matches and destination is None:
        return []

    destination_matches = (
        _run_notice_messages(destination, response_turn_id=response_turn_id) if destination is not None else []
    )
    notice_text = _queued_notice_text_to_persist(
        destination_matches=destination_matches,
    )
    if (
        notice_text is not None
        and len(all_matches) == 1
        and len(destination_matches) == 1
        and queued_notice_marker(destination_matches[0]) == QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER
        and destination_matches[0].content == notice_text
    ):
        return []

    insertion_index: int | None = None
    if destination is not None and destination.messages:
        insertion_index = next(
            (
                index
                for index, message in enumerate(destination.messages)
                if _is_queued_notice_message(
                    message,
                    response_turn_id=response_turn_id,
                )
            ),
            None,
        )

    edited: dict[int, AnyRunRecord] = {}
    for run in runs:
        if not _run_notice_messages(run, response_turn_id=response_turn_id):
            continue
        edited_run = deepcopy(run)
        _strip_queued_notice_messages(edited_run.messages, response_turn_id=response_turn_id)
        edited[id(run)] = edited_run

    if destination is None or notice_text is None:
        return list(edited.values())
    edited_destination = edited.get(id(destination))
    if edited_destination is None:
        edited_destination = deepcopy(destination)
        edited[id(destination)] = edited_destination
    persisted_notice = _new_persisted_queued_notice(response_turn_id, notice_text)
    if insertion_index is None:
        edited_destination.messages.append(persisted_notice)
    else:
        edited_destination.messages.insert(
            min(insertion_index, len(edited_destination.messages)),
            persisted_notice,
        )
    return list(edited.values())


def _finalize_queued_notice_in_session(storage: ConversationStore, *, session_id: str, response_turn_id: str) -> None:
    """Finalize one response in session ``session_id``, editing the messages of a fresh load."""
    storage.rewrite_run_messages(
        session_id,
        partial(_finalize_queued_notice_in_runs, response_turn_id=response_turn_id),
    )


def register_scope_notice_storage(*, scope_context: ScopeSessionContext | None, entity_name: str) -> None:
    """Register the scope one response ran in for queued-notice finalization; the first registration wins.

    A response without a scope registers none.
    """
    notice_context = _queued_message_notice_context.get()
    if notice_context is None or scope_context is None:
        return
    session_id = scope_context.session_id
    session_kind = scope_context.scope.kind
    notice_context.storage_targets.setdefault(
        (entity_name, session_id, session_kind),
        _QueuedNoticeStorageTarget(
            storage_factory=scope_context.storage_factory,
            session_id=session_id,
            session_kind=session_kind,
            entity_name=entity_name,
        ),
    )


async def _finalize_queued_notice_storage_targets(
    targets: Sequence[_QueuedNoticeStorageTarget],
    response_turn_id: str,
) -> None:
    """Finalize all durable targets for one response, each in its database's lane."""
    for target in targets:
        try:
            await run_session_storage_operation(
                target.storage_factory,
                partial(
                    _finalize_queued_notice_in_session,
                    session_id=target.session_id,
                    response_turn_id=response_turn_id,
                ),
            )
        except Exception:
            logger.exception(
                "Failed to finalize queued-message notice in session history",
                entity=target.entity_name,
                session_id=target.session_id,
                session_type=target.session_kind,
                response_turn_id=response_turn_id,
            )


async def finalize_queued_notice_response_turn_async(
    notice_context: _QueuedMessageNoticeContext,
) -> None:
    """Finalize one delivered notice at the user-visible response boundary.

    Cancelling the caller waits for every target to finish before the cancellation propagates.
    """
    if not notice_context.notice_fired or not notice_context.storage_targets:
        return
    await run_coroutine_until_complete(
        _finalize_queued_notice_storage_targets(
            tuple(notice_context.storage_targets.values()),
            notice_context.response_turn_id,
        ),
    )


def _queued_notice_response_turn_ids(
    runs: Sequence[AnyRunRecord],
) -> set[str]:
    return {
        response_turn_id
        for run in runs
        for message in _run_notice_messages(run, response_turn_id=None)
        if (response_turn_id := _queued_notice_response_turn_id(message)) is not None
    }


def _has_notice_marker_for_response(
    runs: Sequence[AnyRunRecord],
    *,
    response_turn_id: str,
    marker: bool | str,
) -> bool:
    return any(
        queued_notice_marker(message) == marker
        for run in runs
        for message in _run_notice_messages(run, response_turn_id=response_turn_id)
    )


def _recover_prior_queued_notices(
    runs: Sequence[AnyRunRecord],
    *,
    active_response_turn_id: str | None,
) -> list[AnyRunRecord]:
    """Return copies of the runs whose crash-left notices from earlier responses are finalized."""
    runs = list(runs)
    changed: dict[str | None, AnyRunRecord] = {}
    for response_turn_id in _queued_notice_response_turn_ids(runs):
        if response_turn_id == active_response_turn_id:
            continue
        if not _has_notice_marker_for_response(
            runs,
            response_turn_id=response_turn_id,
            marker=True,
        ):
            continue
        if _has_notice_marker_for_response(
            runs,
            response_turn_id=response_turn_id,
            marker=QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER,
        ):
            continue
        for edited_run in _finalize_queued_notice_in_runs(runs, response_turn_id=response_turn_id):
            changed[edited_run.run_id] = edited_run
            # Later response turns must edit the already-edited copy, not the stored run.
            runs = [edited_run if run.run_id == edited_run.run_id else run for run in runs]
    return list(changed.values())


def scrub_queued_notice_session_context(
    *,
    scope_context: ScopeSessionContext | None,
    entity_name: str,
) -> None:
    """Recover prior crash-left notices without touching the active response."""
    if scope_context is None or scope_context.session is None:
        return
    session = scope_context.session
    notice_context = _queued_message_notice_context.get()
    recover = partial(
        _recover_prior_queued_notices,
        active_response_turn_id=notice_context.response_turn_id if notice_context is not None else None,
    )
    try:
        # Only a loaded run holding a crash-left notice makes the reply rewrite stored messages.
        if recover(session.runs):
            scope_context.storage.rewrite_run_messages(session.session_id, recover, session=session)
    except Exception:
        logger.exception(
            "Failed to recover queued-message notice in loaded session history",
            entity=entity_name,
            session_id=session.session_id,
            session_type=session.kind,
        )
