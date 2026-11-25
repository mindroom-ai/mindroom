"""Compact a long turn between two of its model requests.

Before every provider request the request is sized (``history.request_size``). When it would exceed the replay window
minus ``reserve_tokens``, everything except the leading system messages, the current prompt, the current turn's transient
messages, and queued-message notices is folded into one summary, and the turn continues from
``[system, current prompt, summary, ...]``. A request without persisted replay keeps that summary as an ordinary
message of its own run.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.agent_loop.model_loop import SendRequest
from mindroom.constants import AI_RUN_METADATA_KEY, PRIOR_USAGE_BY_RUN_METADATA_KEY, PRIOR_USAGE_RUN_METADATA_KEY
from mindroom.helper_usage import get_helper_usage_owner
from mindroom.history.compaction import compact_running_turn, scope_usage_recorder
from mindroom.history.conversation_text import is_own_thread_message
from mindroom.history.policy import resolve_history_preparation_inputs
from mindroom.history.replay import HistorySummaryBudgetError
from mindroom.history.request_size import request_sizer
from mindroom.history.runtime import run_scope_compaction_with_lifecycle
from mindroom.history.storage import new_scope_session, read_fold_basis
from mindroom.history.summarizer import load_summarizer, summarize_runs
from mindroom.history.summary_fork import SummaryFork, holds_fold_basis, remember_fork_frame
from mindroom.history.summary_message import (
    compaction_summary_message,
    compaction_summary_text,
    is_compaction_summary,
    is_scope_summary,
    mark_summary_follows,
)
from mindroom.history.types import owner_scope
from mindroom.llm.messages import is_queued_notice_message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.logging_config import get_logger
from mindroom.session_storage.summary_rows import Fold, summarized_tool_call_ids
from mindroom.token_counting.counts import count, fit_text, utf8_byte_bound
from mindroom.usage.storage import COMPACTED_REQUESTS_METADATA_KEY, carry_request_usage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from typing import Any

    from mindroom.agent_loop.model_loop import BeforeRequestHook, LoopState, RequestDecision
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.request_size import RequestSizer
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.history.types import (
        CompactionLifecycle,
        CompactionLifecycleProgress,
        CompactionOutcome,
        HistoryPreparationInputs,
        HistoryScope,
        ResolvedHistorySettings,
        ResolvedReplayPlan,
    )
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelIdentity, ModelProvider, ModelRequest
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import MessageMetrics
    from mindroom.run_storage import RunStorage
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession

logger = get_logger(__name__)

# Run metadata keys holding usage that belongs to the run whose record holds them, which a run seeded from it drops.
_PRIOR_USAGE_KEYS = (PRIOR_USAGE_RUN_METADATA_KEY, PRIOR_USAGE_BY_RUN_METADATA_KEY)
_RUN_USAGE_METADATA_KEYS = frozenset({COMPACTED_REQUESTS_METADATA_KEY, *_PRIOR_USAGE_KEYS})


@dataclass
class _MidTurnCompaction:
    """One run's mid-turn compaction policy and the state it keeps across that run's requests.

    ``scope`` is the history scope the run compacts as, under which its summaries are made and their usage recorded.
    ``storage`` is where the run stores, and ``replay_plan`` is the plan its requests replay the scope's stored history
    under: a fold appends a summary row to the scope when they replay some, and a run that replays none keeps its
    summary as a message of its own. ``prompt_id`` names the turn's prompt, which every fold keeps in place. Each
    request supplies whether the provider compacts natively, which a text fallback turns off for later requests, and
    which tools tool search defers.
    """

    config: Config
    runtime_paths: RuntimePaths
    scope: HistoryScope
    model: ModelProvider
    policy: _Policy | None
    storage: RunStorage | None
    replay_plan: ResolvedReplayPlan | None
    prompt_id: str | None
    lifecycle: CompactionLifecycle | None
    turn_recorder: TurnRecorder | None
    # Set when compaction failed or cannot bring the request under the limit; this run is not compacted again. An
    # approval resume builds a new hook, so it may try once more.
    stopped: bool = False

    @property
    def scope_fold(self) -> tuple[ConversationStore, ConversationSession] | None:
        """Return the store and session a fold appends its summary row to, checked once for the fold to use.

        None when the run's requests replay no history or the run stores in no scope, so its folds stay within it.
        """
        storage = self.storage
        if storage is None or storage.store is None or storage.scope is None:
            return None
        replays_history = self.replay_plan is not None and self.replay_plan.add_history_to_context
        return (storage.store, storage.session) if replays_history else None


@dataclass(frozen=True)
class _Layout:
    """How one request divides into kept and folded messages."""

    leading: int
    kept: tuple[Message, ...]
    # Replayed messages of earlier turns, which the scope summary or the stored runs it folds already cover.
    history: tuple[Message, ...]
    # This turn's folded messages, oldest first, with each tool batch whole.
    folded: tuple[Message, ...]
    prompt: Message | None
    previous_run_local_summary: str | None


def mid_turn_compaction_hook(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    scope: HistoryScope,
    model_name: str,
    model: ModelProvider,
    history_settings: ResolvedHistorySettings,
    storage: RunStorage | None,
    replay_plan: ResolvedReplayPlan | None,
    prompt_id: str | None,
    lifecycle: CompactionLifecycle | None,
    turn_recorder: TurnRecorder | None,
    inputs: HistoryPreparationInputs | None = None,
    active_context_window: int | None = None,
) -> BeforeRequestHook:
    """Return the pre-request hook that compacts one run's long turn before a request that would exceed its window.

    A fold lands in the scope of ``storage`` when the run's requests replay its stored history under ``replay_plan``;
    ``lifecycle`` shows the fold through the reply's notices and ``turn_recorder`` records the folded calls for an
    interrupted record. Resolves compaction policy and sizer once for the run attempt.
    """
    resolved = inputs or resolve_history_preparation_inputs(
        config=config,
        history_settings=history_settings,
        active_model_name=model_name,
        active_context_window=active_context_window,
    )
    plan = resolved.execution_plan
    policy = (
        _Policy(
            inputs=resolved,
            limit=plan.limit_tokens,
            fit_limit=plan.fit_limit_tokens,
            sizer=request_sizer(model, config, plan.active_model_name or model_name),
        )
        if plan.text_compaction_available
        else None
    )
    compaction = _MidTurnCompaction(
        config=config,
        runtime_paths=runtime_paths,
        scope=scope,
        model=model,
        policy=policy,
        storage=storage,
        replay_plan=replay_plan,
        prompt_id=prompt_id,
        lifecycle=lifecycle,
        turn_recorder=turn_recorder,
    )

    async def hook(state: LoopState) -> RequestDecision:
        await _prepare_request(compaction, state)
        return SendRequest()

    return hook


def without_run_usage(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a run's metadata for seeding a new run, without the usage and prepared context that belong to that run.

    That is the usage of the requests its mid-turn folds carried out of its messages, the usage its turn spent before
    it (``with_prior_usage``), and the prepared context it published, which the new run publishes for its own request.
    """
    if metadata is None:
        return None
    seeded = {key: value for key, value in metadata.items() if key not in _RUN_USAGE_METADATA_KEYS}
    ai_run = seeded.get(AI_RUN_METADATA_KEY)
    if isinstance(ai_run, dict):
        seeded[AI_RUN_METADATA_KEY] = {key: value for key, value in ai_run.items() if key != "prepared_context"}
    return seeded


@dataclass(frozen=True)
class _Fold:
    """A fold's summary message, estimated rewritten size, and optional deferred commit callback."""

    summary_message: Message
    rewritten_tokens: int
    commit: Callable[[], None] | None = None


@dataclass(frozen=True)
class _Policy:
    """The resolved compaction plan and request limit for one model request."""

    inputs: HistoryPreparationInputs
    limit: int | None
    # The model's own window minus the reserve; a rewritten request over it would fail at the provider.
    fit_limit: int | None
    # How the requests of the run's loop are sized.
    sizer: RequestSizer


async def _prepare_request(compaction: _MidTurnCompaction, state: LoopState) -> None:
    messages = state.messages
    live_request = state.request()
    scope_fold = compaction.scope_fold
    if scope_fold is not None:
        store, session = scope_fold
        leading = next((index for index, message in enumerate(messages) if message.role != "system"), len(messages))
        remember_fork_frame(
            (store.path, owner_scope(session).key, session.session_id),
            live_request,
            messages[:leading],
        )
    requested = _consume_request(state.run.compaction_requests, compaction.scope)
    if (compaction.stopped and not requested) or (policy := compaction.policy) is None:
        return
    if not requested and (not policy.inputs.execution_plan.automatic_compaction or policy.limit is None):
        return
    # Sizing can tokenize the whole request, so it stays off the event loop that serves every room.
    request_size = await asyncio.to_thread(policy.sizer.size, live_request)
    if not requested and (policy.limit is None or request_size.tokens <= policy.limit):
        return
    layout = _layout(messages, compaction.prompt_id, scoped=scope_fold is not None)
    if not layout.folded and all(is_compaction_summary(message) for message in layout.history):
        # Nothing but an earlier summary would fold, so compacting again cannot shrink the request.
        return
    fold = await _summarize(
        compaction,
        policy,
        layout,
        messages,
        state.record,
        scope_fold,
        request_size.tokens,
        live_request,
        # The rewritten request replays no checkpoint, which the summary replaced.
        lambda rewritten: compaction.model.request_tokens(
            replace(state.request(rewritten), native_compaction=None),
        ),
    )
    if fold is None:
        compaction.stopped = True
        return
    _require_fit(policy, fold.rewritten_tokens, fold.summary_message)
    if fold.commit is not None:
        fold.commit()
    _disable_native_compaction(state, requested=requested)
    if policy.limit is not None and fold.rewritten_tokens > policy.limit:
        # What stays (system prompt, tools, summary, prompt) exceeds the replay window on its own; compacting again
        # would only summarize each new tool result before the model ever saw it.
        logger.warning(
            "Compacted request still exceeds the replay window; not compacting again",
            run_id=state.record.run_id,
        )
        compaction.stopped = True


def _consume_request(requests: set[HistoryScope] | None, scope: HistoryScope) -> bool:
    if requests is None or scope not in requests:
        return False
    requests.discard(scope)
    return True


def _disable_native_compaction(state: LoopState, *, requested: bool) -> None:
    if state.request_template.native_compaction is None:
        return
    if requested:
        logger.info(
            "Mid-turn compaction requested on native compaction route; compacted as text",
            run_id=state.record.run_id,
        )
    else:
        # The provider's own compaction could not keep this request within the window.
        logger.warning(
            "Native compaction left the request over its limit; compacted as text",
            run_id=state.record.run_id,
        )
    # The fold replaced the checkpoints the provider's compaction replayed, so the loop's later requests carry no
    # native settings: each route sends them as plain requests, and this hook sizes and compacts them as text.
    state.request_template = replace(state.request_template, native_compaction=None)


def _mid_turn_fork(policy: _Policy, live_request: ModelRequest) -> SummaryFork | None:
    plan = policy.inputs.execution_plan
    if plan.compaction_context_window is None:
        return None
    return SummaryFork(
        sizer=policy.sizer,
        request=live_request,
        context_window=plan.compaction_context_window,
        reserve_tokens=plan.reserve_tokens,
        mid_turn=True,
    )


async def _summarize(
    compaction: _MidTurnCompaction,
    policy: _Policy,
    layout: _Layout,
    messages: list[Message],
    record: AnyRunRecord,
    scope_fold: tuple[ConversationStore, ConversationSession] | None,
    request_tokens: int,
    live_request: ModelRequest,
    size_request: Callable[[list[Message]], int],
) -> _Fold | None:
    """Fold the history and every exchange of the turn into a summary and rewrite the request.

    ``request_tokens`` sized the request before the fold and ``size_request`` estimates a request of given messages.
    Returns the summary message with the rewritten request's estimate, or None when compaction failed or no summary
    could fit, and the request is unchanged.
    """
    # Replay limits trim history; the running turn's own tool results must all reach the summary.
    settings = replace(policy.inputs.history_settings, max_tool_calls_from_history=None)
    inputs = replace(policy.inputs, history_settings=settings)
    try:
        # What the fold keeps is this reply's static prompt, so the summary gets the history budget left beside it.
        kept_tokens = await asyncio.to_thread(size_request, [*messages[: layout.leading], *layout.kept])
        if scope_fold is None:
            budget_limit = policy.limit if policy.limit is not None and kept_tokens < policy.limit else policy.fit_limit
            # Without a stored session to append a summary row to, replayed history is summarized with the turn's
            # exchanges, each message as a snapshot of its own.
            snapshots = [
                *(_snapshot_run(record, [message], None) for message in layout.history),
                *_snapshots(record, layout, inputs),
            ]
            summary, _ = await summarize_runs(
                load_summarizer(
                    inputs.execution_plan,
                    inputs.history_settings,
                    config=compaction.config,
                    runtime_paths=compaction.runtime_paths,
                    session_id=record.session_id or "",
                    scope=compaction.scope,
                    on_usage=_summary_usage_recorder(compaction, record),
                ),
                layout.previous_run_local_summary,
                snapshots,
                fork=_mid_turn_fork(policy, live_request),
                max_summary_tokens=None if budget_limit is None else max(0, budget_limit - kept_tokens),
                model=compaction.model,
            )
            summary_message = compaction_summary_message(summary, run_local=True)
            rewritten, prompt_to_mark = _rewritten_messages(messages, layout, summary_message)
            # Sizing can tokenize the whole request, so it stays off the event loop that serves every room.
            rewritten_tokens = await asyncio.to_thread(size_request, rewritten)
            return _Fold(
                summary_message,
                rewritten_tokens,
                commit=partial(_apply_rewrite, messages, rewritten, prompt_to_mark, layout, record),
            )
        store, session = scope_fold
        if policy.limit is not None and kept_tokens >= policy.limit:
            # What the fold keeps, such as the prompt's attachments, fills the limit alone, so no summary could fit.
            logger.warning(
                "Request exceeds the replay window without its history; not compacting",
                run_id=record.run_id,
            )
            return None
        return await _compact_scope(
            compaction,
            policy,
            inputs,
            layout,
            messages,
            record,
            request_tokens,
            live_request,
            size_request,
            store=store,
            session=session,
            max_summary_tokens=None if policy.limit is None else policy.limit - kept_tokens,
        )
    except Exception:
        logger.exception("Mid-turn compaction failed; continuing without it", run_id=record.run_id)
        return None


async def _compact_scope(
    compaction: _MidTurnCompaction,
    policy: _Policy,
    inputs: HistoryPreparationInputs,
    layout: _Layout,
    messages: list[Message],
    record: AnyRunRecord,
    request_tokens: int,
    live_request: ModelRequest,
    size_request: Callable[[list[Message]], int],
    *,
    store: ConversationStore,
    session: ConversationSession,
    max_summary_tokens: int | None,
) -> _Fold | None:
    """Append one summary row to ``session``'s scope, covering its runs after the latest row and this run's exchanges.

    The append stores the run's folded part with the row and leaves the turn's session as the store holds it, and
    once the row is stored, the run's request and record follow. A summary over ``max_summary_tokens`` is not
    stored, and the request stays as it was.
    """
    basis = await asyncio.to_thread(read_fold_basis, store, session)
    if basis is None:
        logger.warning("Mid-turn compaction skipped because the conversation changed", run_id=record.run_id)
        return None
    scope = owner_scope(session)
    basis_matches = await asyncio.to_thread(
        holds_fold_basis,
        layout.history,
        basis_summary=basis.summary,
        basis_runs=basis.runs,
        scope=scope,
    )
    fork = _mid_turn_fork(policy, live_request) if basis_matches else None
    if not basis_matches:
        logger.info(
            "Mid-turn compaction fork skipped; falling back to XML summary",
            run_id=record.run_id,
            session_id=session.session_id,
            scope=scope.key,
            compaction_strategy="xml",
            fork_skip_reason="fold_basis_mismatch",
        )
    fold = _fold(record, layout)
    folded_calls = _folded_calls(fold)
    prepared: tuple[Message, list[Message], Message | None] | None = None
    committed_summary: list[Message] = []

    def size_rewritten(summary: str) -> int:
        nonlocal prepared
        summary_message = compaction_summary_message(summary, run_local=False)
        rewritten, prompt_to_mark = _rewritten_messages(messages, layout, summary_message)
        after_tokens = size_request(rewritten)
        prepared = (summary_message, rewritten, prompt_to_mark)
        return after_tokens

    def on_commit(_summary: str) -> None:
        # Runs together with the row's commit, so a stopped or failed reply's record leaves the folded calls out.
        assert prepared is not None
        summary_message, rewritten, prompt_to_mark = prepared
        committed_summary.append(summary_message)
        if compaction.turn_recorder is not None:
            compaction.turn_recorder.record_folded_tool_calls(folded_calls)
        _apply_rewrite(messages, rewritten, prompt_to_mark, layout, record)

    plan = inputs.execution_plan
    snapshots = _snapshots(record, layout, inputs)

    async def compact(
        _progress: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
        complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    ) -> CompactionOutcome:
        # One fold writes one row, so there is no per-chunk progress to report.
        return await compact_running_turn(
            storage=store,
            session=session,
            basis=basis,
            fold=fold,
            snapshots=snapshots,
            summarizer=load_summarizer(
                plan,
                inputs.history_settings,
                config=compaction.config,
                runtime_paths=compaction.runtime_paths,
                session_id=session.session_id,
                scope=scope,
                # A first turn's fold records its summary usage before any run of the conversation is stored.
                on_usage=scope_usage_recorder(
                    store,
                    session.session_id,
                    initial_session=new_scope_session(session_id=session.session_id, scope=scope),
                ),
            ),
            # The size that triggered this compaction, so the history budget sees what the request needs.
            before_tokens=request_tokens,
            replay_window_tokens=plan.replay_window_tokens,
            threshold_tokens=plan.trigger_threshold_tokens,
            available_history_budget=plan.limit_tokens,
            max_summary_tokens=max_summary_tokens,
            model=compaction.model,
            on_commit=on_commit,
            # Size the prospective rewritten request before SQLite commit.
            rewritten_request_tokens=size_rewritten,
            complete=complete,
            fork=fork,
        )

    result = await run_scope_compaction_with_lifecycle(
        mode="auto",
        session_id=session.session_id,
        scope=scope,
        execution_plan=plan,
        history_budget=plan.limit_tokens,
        current_history_tokens=request_tokens,
        runs_before=len(basis.runs) + 1,
        compaction_lifecycle=compaction.lifecycle,
        compact=compact,
    )
    if result.outcome is None or not committed_summary:
        return None
    return _Fold(committed_summary[0], result.outcome.after_tokens)


def _fold(record: AnyRunRecord, layout: _Layout) -> Fold:
    """Return what this fold takes out of the running run, from the live request and run tree.

    The run's messages are written only when the run ends, so the request's folded exchanges and the member runs that
    ended so far are the fold's part of the turn; a member an earlier fold of the run took stays that fold's.
    """
    members = record.ended_member_runs if isinstance(record, TeamRunRecord) else []
    return Fold(run=record, messages=layout.folded, member_run_ids=frozenset(member.run_id for member in members))


def _folded_calls(fold: Fold) -> set[str]:
    """Return the tool calls ``fold`` summarizes, as the store reads them back from the rows the fold flags."""
    members = fold.run.ended_member_runs if isinstance(fold.run, TeamRunRecord) else []
    return summarized_tool_call_ids(
        [message.to_dict() for message in fold.messages],
        [tool.to_dict() for member in members if member.run_id in fold.member_run_ids for tool in member.tools or []],
    )


def _require_fit(policy: _Policy, tokens: int, summary_message: Message) -> None:
    """Refuse to send a rewritten request whose kept messages and summary exceed the model's own window."""
    if policy.fit_limit is None or tokens <= policy.fit_limit:
        return
    summary_tokens = count(policy.sizer.model.token_counter, str(summary_message.content))
    raise HistorySummaryBudgetError(
        summary_tokens=summary_tokens,
        available_tokens=max(0, policy.fit_limit - (tokens - summary_tokens)),
    )


def _layout(messages: list[Message], prompt_id: str | None, *, scoped: bool) -> _Layout:
    """Split a request into kept messages, replayed history, and this turn's messages to fold.

    A run-local summary this run already wrote seeds the next run-local summary; a scoped compaction folds it into
    the snapshots instead, since the scope summary never saw it.
    """
    leading = next(
        (index for index, message in enumerate(messages) if message.role != "system"),
        len(messages),
    )
    prompt = _current_prompt(messages[leading:], prompt_id)
    kept: list[Message] = []
    history: list[Message] = []
    folded: list[Message] = []
    previous: str | None = None
    for exchange in _exchanges(messages[leading:]):
        head = exchange[0]
        if head is prompt or (
            not head.from_history and (not head.add_to_agent_memory or is_queued_notice_message(head))
        ):
            kept.extend(exchange)
        elif is_compaction_summary(head) and not head.from_history and not scoped:
            previous = compaction_summary_text(head)
        elif head.from_history:
            history.extend(exchange)
        else:
            folded.extend(exchange)
    return _Layout(
        leading=leading,
        kept=tuple(kept),
        history=tuple(history),
        folded=tuple(folded),
        prompt=prompt,
        previous_run_local_summary=previous,
    )


def _exchanges(messages: Sequence[Message]) -> list[list[Message]]:
    """Group each message with the tool results that answer it, so a tool batch is never split.

    A tool result joins the latest assistant tool call even when another message sits between them; that message
    stays its own exchange.
    """
    exchanges: list[list[Message]] = []
    calls: list[Message] | None = None
    for message in messages:
        if message.role == "tool" and calls is not None:
            calls.append(message)
            continue
        exchanges.append([message])
        if message.role == "assistant":
            calls = exchanges[-1] if message.tool_calls else None
    return exchanges


def _current_prompt(messages: Sequence[Message], prompt_id: str | None) -> Message | None:
    """Return the turn's current user message: the one bound by id, else the first stored user message of the turn."""
    if prompt_id is not None:
        match = next((message for message in reversed(messages) if message.id == prompt_id), None)
        if match is not None:
            return match
    return next(
        (
            message
            for message in messages
            if message.role == "user"
            and not message.from_history
            and message.add_to_agent_memory
            and not is_compaction_summary(message)
            and not is_queued_notice_message(message)
            and not is_own_thread_message(message)
        ),
        None,
    )


def _snapshots(record: AnyRunRecord, layout: _Layout, inputs: HistoryPreparationInputs) -> list[RunRecord]:
    """Return one completed run per folded message, oldest first, the first led by the prompt and the run's metadata.

    Separate runs let the summary chunker size the turn with the summary model's own estimator and summarize it in
    as many chunks as it needs, without truncating any message that fits a summary input by itself. The request keeps
    a tool batch whole, but a parallel batch can exceed one summary input; each tool result names its own tool and
    arguments in the summary input, so it needs no call message beside it.
    """
    snapshots: list[RunRecord] = []
    budget_tokens = inputs.execution_plan.summary_input_budget_tokens or 0
    folded = layout.folded
    for index, message in enumerate(folded):
        lead = (_prompt_context(layout.prompt, budget_tokens),) if index == 0 and layout.prompt is not None else ()
        # The run's metadata is the same for every message, so only the first snapshot shows it to the summarizer.
        metadata = deepcopy(record.metadata) if index == 0 else None
        snapshots.append(_snapshot_run(record, [*lead, message], metadata))
    return snapshots


def _snapshot_run(record: AnyRunRecord, messages: Sequence[Message], metadata: dict[str, Any] | None) -> RunRecord:
    """Return a completed copy of part of the running turn under a fresh snapshot run id."""
    return RunRecord(
        run_id=f"{record.run_id}:fold:{uuid4().hex}",
        session_id=record.session_id,
        status=RunStatus.completed,
        messages=[deepcopy(message) for message in messages],
        metadata=metadata,
        created_at=record.created_at,
    )


def _prompt_context(prompt: Message, budget_tokens: int) -> Message:
    """Return a copy of the prompt for the summarizer's context, bounded so the message beside it always fits.

    The prompt stays in the request verbatim, so the summary only needs enough of it to know what the work was for:
    at most a quarter of the input, counted in UTF-8 bytes, which no summary model's tokenizer exceeds.
    """
    context = deepcopy(prompt)
    if isinstance(context.content, str):
        context.content = fit_text(
            context.content,
            max(budget_tokens // 4, 1),
            utf8_byte_bound,
            notice="\n[The rest of this request is unchanged in the conversation.]",
        )
    return context


def _summary_usage_recorder(
    compaction: _MidTurnCompaction,
    record: AnyRunRecord,
) -> Callable[[ModelIdentity, MessageMetrics], Awaitable[None]] | None:
    """Record each summary response under the reply's usage owner, else in the run's own session storage.

    The usage is recorded under the current request's requester, as a compaction before the reply records its own.
    """
    if (owner := get_helper_usage_owner()) is not None:
        return scope_usage_recorder(owner.storage_factory, owner.session_id, initial_session=owner.initial_session)
    storage = compaction.storage.store if compaction.storage is not None else None
    if storage is None or not record.session_id:
        logger.warning("Mid-turn summary usage has no storage owner", run_id=record.run_id)
        return None
    return scope_usage_recorder(
        storage,
        record.session_id,
        # The run's own session row may not exist yet while its first turn is still running.
        initial_session=new_scope_session(session_id=record.session_id, scope=compaction.scope),
    )


def _rewritten_messages(
    messages: Sequence[Message],
    layout: _Layout,
    summary_message: Message,
) -> tuple[list[Message], Message | None]:
    """Build the rewritten message list without mutating ``messages``, plus the prompt to mark on commit if any."""
    after_prompt = next((index + 1 for index, message in enumerate(layout.kept) if message is layout.prompt), 0)
    prompt_to_mark = layout.kept[after_prompt - 1] if after_prompt and is_scope_summary(summary_message) else None
    rewritten = [*messages[: layout.leading], *layout.kept[:after_prompt], summary_message, *layout.kept[after_prompt:]]
    return rewritten, prompt_to_mark


def _apply_rewrite(
    messages: list[Message],
    rewritten: list[Message],
    prompt_to_mark: Message | None,
    layout: _Layout,
    record: AnyRunRecord,
) -> None:
    """Apply a prepared rewrite to live ``messages`` and ``record`` after validation/commit succeeds."""
    if prompt_to_mark is not None:
        mark_summary_follows(prompt_to_mark)
    messages[:] = rewritten
    carry_request_usage(record, layout.folded)


__all__ = ["mid_turn_compaction_hook", "without_run_usage"]
