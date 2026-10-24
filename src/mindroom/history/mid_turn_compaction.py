"""Compact a long turn between two of its model requests.

Before every provider request the request is sized. When it would exceed the replay window minus
``reserve_tokens``, everything except the leading system messages, the current prompt, transient messages, and
queued-message notices is folded into one summary, and the turn continues from
``[system, current prompt, summary, ...]``. A request without persisted replay keeps that summary as an ordinary
message of its own run.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.agent_loop.model_loop import SendRequest
from mindroom.helper_usage import get_helper_usage_owner
from mindroom.history.legacy_summary_system_prompt import strip_embedded_summary
from mindroom.history.policy import context_budget_after_reserve
from mindroom.history.replay import (
    HistorySummaryBudgetError,
    compaction_summary_message,
    estimate_request_messages_tokens,
)
from mindroom.history.run_visibility import is_queued_notice_message
from mindroom.history.runtime import (
    compact_scope_mid_turn,
    reported_context_tokens,
    resolve_entity_preparation_inputs,
    summarize_run_locally,
)
from mindroom.history.storage import new_scope_session, read_fold_basis, record_summary_usage
from mindroom.history.summary_message import (
    compaction_summary_text,
    is_compaction_summary,
    is_scope_summary,
    mark_summary_follows,
)
from mindroom.history.summary_rows import Segment, folded_tool_call_ids
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import is_own_model_message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.logging_config import get_logger
from mindroom.native_compaction import checkpoint_items, native_replay_messages
from mindroom.providers.anthropic.prompt_cache import claude_loaded_tool_names
from mindroom.session_storage.agent_storage import save_compaction_usage
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.usage.storage import COMPACTED_REQUESTS_METADATA_KEY, project_requests

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from typing import Any

    from mindroom.agent_loop.model_loop import BeforeRequestHook, LoopState, RequestDecision
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.runtime import HistoryPreparationInputs
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.history.types import CompactionLifecycle, ResolvedHistorySettings, ResolvedReplayPlan
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelIdentity, ModelProvider, ToolSpec
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import MessageMetrics
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession

logger = get_logger(__name__)


@dataclass
class _MidTurnCompaction:
    """One run's mid-turn compaction policy and the state it keeps across that run's requests.

    ``store``, ``session``, and ``scope`` are where the run's scope stores, and ``replay_plan`` is the plan its
    requests replay stored history under: a fold appends a summary row to the scope when they replay some, and a run
    that replays none keeps its summary as a message of its own. ``prompt_id`` names the turn's prompt, which every
    fold keeps in place. Whether the provider compacts natively is read from each request, which a fallback changes.
    """

    config: Config
    runtime_paths: RuntimePaths
    entity_name: str | None
    scope_id: str
    is_team: bool
    model_name: str
    model: ModelProvider
    history_settings: ResolvedHistorySettings
    store: ConversationStore | None
    session: ConversationSession | None
    scope: HistoryScope | None
    replay_plan: ResolvedReplayPlan | None
    deferred_tool_names: frozenset[str]
    prompt_id: str | None
    lifecycle: CompactionLifecycle | None
    turn_recorder: TurnRecorder | None
    # Set when compaction failed or cannot bring the request under the limit; this run is not compacted again. An
    # approval resume builds a new hook, so it may try once more.
    stopped: bool = False
    # Member runs an earlier fold of this run already stored in its segment.
    folded_member_ids: set[str] = field(default_factory=set)

    @property
    def scoped_session(self) -> ConversationSession | None:
        """Return the session a fold appends its summary row to, or None when the run's requests replay no history."""
        replays_history = self.replay_plan is not None and self.replay_plan.add_history_to_context
        return self.session if replays_history and self.store is not None and self.scope is not None else None


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
    entity_name: str | None,
    scope_id: str,
    is_team: bool,
    model_name: str,
    model: ModelProvider,
    history_settings: ResolvedHistorySettings,
    store: ConversationStore | None,
    session: ConversationSession | None,
    scope: HistoryScope | None,
    replay_plan: ResolvedReplayPlan | None,
    deferred_tool_names: frozenset[str],
    prompt_id: str | None,
    lifecycle: CompactionLifecycle | None = None,
    turn_recorder: TurnRecorder | None = None,
) -> BeforeRequestHook:
    """Return the pre-request hook that compacts one run's long turn before a request that would exceed its window.

    A fold lands in ``scope`` when the run's requests replay its stored history under ``replay_plan``; ``lifecycle``
    shows the fold through the reply's notices and ``turn_recorder`` records the folded calls for an interrupted
    record.
    """
    binding = _MidTurnCompaction(
        config=config,
        runtime_paths=runtime_paths,
        entity_name=entity_name,
        scope_id=scope_id,
        is_team=is_team,
        model_name=model_name,
        model=model,
        history_settings=history_settings,
        store=store,
        session=session,
        scope=scope,
        replay_plan=replay_plan,
        deferred_tool_names=deferred_tool_names,
        prompt_id=prompt_id,
        lifecycle=lifecycle,
        turn_recorder=turn_recorder,
    )

    async def hook(state: LoopState) -> RequestDecision:
        await _prepare_request(binding, state)
        return SendRequest()

    return hook


def without_fold_records(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a run's metadata for seeding a new run, without the folded usage that belongs to that run."""
    if metadata is None:
        return None
    return {key: value for key, value in metadata.items() if key != COMPACTED_REQUESTS_METADATA_KEY}


@dataclass(frozen=True)
class _Policy:
    """The resolved compaction plan and request limit for one model request."""

    inputs: HistoryPreparationInputs
    limit: int
    # The model's own window minus the reserve; a rewritten request over it would fail at the provider.
    fit_limit: int
    # Set while the request asks the provider to compact; the hook then only falls back to text when it cannot fit.
    native_route: str | None


async def _prepare_request(binding: _MidTurnCompaction, state: LoopState) -> None:
    if binding.stopped:
        return
    policy = _policy(binding, state)
    if policy is None:
        return
    messages = state.messages
    # Sizing can tokenize the whole request, so it stays off the event loop that serves every room.
    request_tokens = await asyncio.to_thread(
        _request_tokens,
        binding,
        policy,
        messages,
        state.tools,
        state.loop_start_ids,
    )
    if request_tokens <= policy.limit:
        return
    session = binding.scoped_session
    layout = _layout(messages, binding.prompt_id, scoped=session is not None)
    if not layout.folded and all(is_compaction_summary(message) for message in layout.history):
        # Nothing but an earlier summary would fold, so compacting again cannot shrink the request.
        return
    summary_message = await _summarize(binding, policy, layout, messages, state.record, session, request_tokens)
    if summary_message is None:
        binding.stopped = True
        return
    if policy.native_route is not None:
        # The provider's own compaction could not keep this request within the window.
        logger.warning(
            "Native compaction left the request over its limit; compacted as text",
            run_id=state.record.run_id,
        )
        # The fold replaced the checkpoints the provider's compaction replayed, so the loop's later requests carry no
        # native settings: each route sends them as plain requests, and this hook sizes and compacts them as text.
        state.request_template = replace(state.request_template, native_compaction=None)
    rewritten_tokens = await asyncio.to_thread(_estimated_request_tokens, binding, messages, state.tools)
    _require_fit(policy, rewritten_tokens, summary_message)
    if rewritten_tokens > policy.limit:
        # What stays (system prompt, tools, summary, prompt) exceeds the replay window on its own; compacting again
        # would only summarize each new tool result before the model ever saw it.
        logger.warning(
            "Compacted request still exceeds the replay window; not compacting again",
            run_id=state.record.run_id,
        )
        binding.stopped = True


async def _summarize(
    binding: _MidTurnCompaction,
    policy: _Policy,
    layout: _Layout,
    messages: list[Message],
    record: AnyRunRecord,
    session: ConversationSession | None,
    request_tokens: int,
) -> Message | None:
    """Fold the history and every exchange of the turn into a summary and rewrite the request.

    Returns the summary message, or None when compaction failed and the request is unchanged.
    """
    # Replay limits trim history; the running turn's own tool results must all reach the summary.
    settings = replace(policy.inputs.history_settings, max_tool_calls_from_history=None)
    inputs = replace(policy.inputs, history_settings=settings)
    try:
        if session is None:
            summary_message = await _summarize_run_locally(binding, inputs, layout, record)
            _finish(messages, layout, summary_message, record)
            return summary_message
        return await _compact_scope(binding, inputs, layout, messages, record, session, request_tokens)
    except Exception:
        logger.exception("Mid-turn compaction failed; continuing without it", run_id=record.run_id)
        return None


def _finish(messages: list[Message], layout: _Layout, summary_message: Message, record: AnyRunRecord) -> None:
    _rewrite(messages, layout, summary_message)
    _carry_request_usage(record, layout.folded)


async def _compact_scope(
    binding: _MidTurnCompaction,
    inputs: HistoryPreparationInputs,
    layout: _Layout,
    messages: list[Message],
    record: AnyRunRecord,
    session: ConversationSession,
    request_tokens: int,
) -> Message | None:
    """Append one summary row covering the scope's stored runs after the latest row and this run's exchanges.

    The row holds the run's folded part as its segment; the append leaves the turn's session as the store holds it,
    and once the row is stored, the run's request and record follow.
    """
    storage = binding.store
    scope = binding.scope
    assert storage is not None
    assert scope is not None
    basis = await asyncio.to_thread(read_fold_basis, storage, session, scope)
    if basis is None:
        logger.warning("Mid-turn compaction skipped because the conversation changed", run_id=record.run_id)
        return None
    segment = _segment(record, layout, binding.folded_member_ids)
    folded_calls = folded_tool_call_ids(segment.payload())
    summary_messages: list[Message] = []

    def on_commit(summary: str) -> None:
        # Runs together with the row's commit, so a stopped or failed reply's record leaves the folded calls out.
        summary_message = compaction_summary_message(summary, run_local=False)
        summary_messages.append(summary_message)
        binding.folded_member_ids.update(
            member["run_id"] for member in segment.member_responses if member.get("run_id")
        )
        if binding.turn_recorder is not None:
            binding.turn_recorder.record_folded_tool_calls(folded_calls)
        _finish(messages, layout, summary_message, record)

    outcome = await compact_scope_mid_turn(
        storage=storage,
        session=session,
        scope=scope,
        resolved_inputs=inputs,
        basis=basis,
        segment=segment,
        snapshots=_snapshots(record, layout, inputs),
        folded_messages=layout.folded,
        # The size that triggered this compaction, so the history budget sees what the request needs.
        before_tokens=request_tokens,
        config=binding.config,
        runtime_paths=binding.runtime_paths,
        compaction_lifecycle=binding.lifecycle,
        on_commit=on_commit,
    )
    return summary_messages[0] if outcome is not None and summary_messages else None


def _segment(record: AnyRunRecord, layout: _Layout, folded_member_ids: set[str]) -> Segment:
    """Return what this fold takes out of the running run, from the live request and run tree.

    The run's messages are written only when the run ends, so the request's folded exchanges and the member runs
    that ended so far, less those an earlier fold of the run stored, are the fold's part of the turn.
    """
    folded = layout.folded
    call_ids = {
        call["id"] for message in folded for call in message.tool_calls or [] if isinstance(call.get("id"), str)
    }
    members = (
        [member for member in record.ended_member_runs if member.run_id not in folded_member_ids]
        if isinstance(record, TeamRunRecord)
        else []
    )
    return Segment(
        run_id=record.run_id,
        # Folded usage stays in the run's metadata; the segment needs only the events the run read and answered.
        metadata=without_fold_records(deepcopy(record.metadata)) or {},
        messages=[message.to_dict() for message in folded],
        tools=[tool.to_dict() for tool in record.tools or [] if tool.tool_call_id in call_ids],
        member_responses=[member.to_dict() for member in members],
    )


def _policy(binding: _MidTurnCompaction, state: LoopState) -> _Policy | None:
    """Return the limit text compaction keeps requests under, or None when it cannot run for this request."""
    inputs = resolve_entity_preparation_inputs(
        config=binding.config,
        entity_name=binding.entity_name,
        static_prompt_tokens=0,
        active_model_name=binding.model_name,
        active_context_window=None,
        history_settings=binding.history_settings,
    )
    plan = inputs.execution_plan
    if not (plan.authored_compaction_enabled and plan.text_compaction_available) or plan.replay_window_tokens is None:
        return None
    model_window = binding.config.get_model_context_window(binding.model_name)
    native = state.request_template.native_compaction
    return _Policy(
        inputs=inputs,
        limit=context_budget_after_reserve(plan.replay_window_tokens, plan.reserve_tokens),
        fit_limit=context_budget_after_reserve(model_window or plan.replay_window_tokens, plan.reserve_tokens),
        native_route=native.route if native is not None else None,
    )


def _require_fit(policy: _Policy, tokens: int, summary_message: Message) -> None:
    """Refuse to send a rewritten request whose kept messages and summary exceed the model's own window."""
    if tokens <= policy.fit_limit:
        return
    summary_tokens = estimate_text_tokens(str(summary_message.content))
    raise HistorySummaryBudgetError(
        summary_tokens=summary_tokens,
        available_tokens=max(0, policy.fit_limit - (tokens - summary_tokens)),
    )


def _request_tokens(
    binding: _MidTurnCompaction,
    policy: _Policy,
    messages: list[Message],
    tools: Sequence[ToolSpec],
    loaded_ids: frozenset[str],
) -> int:
    """Size the next request from the latest response this loop received, else by estimate.

    A native response that created a checkpoint is billed for the transcript it replaced, so native routes always
    estimate their projected request.
    """
    if policy.native_route is not None:
        projected = native_replay_messages(messages, policy.native_route)
        return (
            estimate_request_messages_tokens(
                messages,
                replay_model=binding.model.replay_estimator,
                native_route=policy.native_route,
            )
            # Thinking before the latest checkpoint is part of that checkpoint, not of the request.
            + _thinking_tokens(
                [message for message in projected if not checkpoint_items(message, policy.native_route)],
            )
            + _tool_tokens(binding, tools, projected)
        )
    anchor = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].role == "assistant" and messages[index].id not in loaded_ids
        ),
        None,
    )
    if anchor is not None:
        reported = reported_context_tokens(
            messages[anchor],
            model=binding.model,
            config=binding.config,
            model_name=binding.model_name,
        )
        if reported is not None:
            newer = messages[anchor:]
            return (
                reported
                + estimate_request_messages_tokens(newer, replay_model=binding.model.replay_estimator)
                + _thinking_tokens(newer)
            )
    return _estimated_request_tokens(binding, messages, tools)


def _estimated_request_tokens(binding: _MidTurnCompaction, messages: list[Message], tools: Sequence[ToolSpec]) -> int:
    return (
        estimate_request_messages_tokens(messages, replay_model=binding.model.replay_estimator)
        + _thinking_tokens(messages)
        + _tool_tokens(binding, tools, messages)
    )


def _tool_tokens(binding: _MidTurnCompaction, tools: Sequence[ToolSpec], messages: Sequence[Message]) -> int:
    """Estimate the tool definitions a request carries in full; tool search keeps deferred ones out until it loads them.

    Claude expands a search's tool references into definitions; OpenAI replays them inside the search output, and
    portable tool search returns them as an ordinary tool result, both of which the message estimate already counts.
    """
    deferred = binding.deferred_tool_names
    if deferred:
        deferred -= claude_loaded_tool_names(messages)
    # TODO(simplify): this sizes each definition inside a Chat ``function`` envelope while the static estimate
    # (``prompt_tokens.py``) sizes the bare fields, so one tool costs two amounts. Size both through one helper.
    # Operators would see compaction trigger, and models see replay trimmed, a few tokens per tool differently.
    payloads = [
        {"type": "function", "function": {key: value for key, value in asdict(tool).items() if value is not None}}
        for tool in tools
        if tool.name not in deferred
    ]
    return estimate_text_tokens(stable_serialize(payloads))


def _thinking_tokens(messages: Sequence[Message]) -> int:
    """Estimate this turn's thinking, which providers resend inside a tool loop but history estimates skip.

    Each of the turn's own model messages (``is_own_model_message``) counts. Encrypted or omitted thinking shows no
    text, so a response's reported output beyond its visible part counts too.
    """
    total = 0
    for message in messages:
        if not is_own_model_message(message):
            continue
        text = estimate_text_tokens((message.reasoning_content or "") + (message.redacted_reasoning_content or ""))
        visible = estimate_request_messages_tokens([message], replay_model=None)
        total += max(text, message.metrics.output_tokens - visible, 0)
    return total


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
        if head is prompt or not head.add_to_agent_memory or (is_queued_notice_message(head) and not head.from_history):
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

    A tool result joins the latest assistant tool call even when a projection, such as an approval receipt, sits
    between them; that projection stays its own exchange.
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

    The prompt stays in the request verbatim, so the summary only needs enough of it to know what the work was for.
    A quarter as many characters as budget tokens keeps it within a quarter of the input even when the summary
    model counts bytes.
    """
    context = deepcopy(prompt)
    limit = max(budget_tokens // 4, 1)
    if isinstance(context.content, str) and len(context.content) > limit:
        context.content = context.content[:limit] + "\n[The rest of this request is unchanged in the conversation.]"
    return context


async def _summarize_run_locally(
    binding: _MidTurnCompaction,
    inputs: HistoryPreparationInputs,
    layout: _Layout,
    record: AnyRunRecord,
) -> Message:
    scope = HistoryScope(kind="team" if binding.is_team else "agent", scope_id=binding.scope_id)
    # Without a stored session to append a summary row to, replayed history is summarized with the turn's exchanges,
    # as content of its own snapshot rather than a copy the summary input skips.
    history = [_snapshot_run(record, [replace(message, from_history=False)], None) for message in layout.history]
    summary = await summarize_run_locally(
        resolved_inputs=inputs,
        previous_summary=layout.previous_run_local_summary,
        snapshots=[*history, *_snapshots(record, layout, inputs)],
        scope=scope,
        config=binding.config,
        runtime_paths=binding.runtime_paths,
        on_usage=_summary_usage_recorder(binding, record),
    )
    return compaction_summary_message(summary, run_local=True)


def _summary_usage_recorder(
    binding: _MidTurnCompaction,
    record: AnyRunRecord,
) -> Callable[[ModelIdentity, MessageMetrics], Awaitable[None]] | None:
    """Record each summary response under the reply's usage owner, else in the run's own session storage."""
    owner = get_helper_usage_owner()
    if owner is not None:

        async def record_for_owner(model: ModelIdentity, usage: MessageMetrics) -> None:
            await run_session_storage_operation(
                owner.storage_factory,
                partial(
                    save_compaction_usage,
                    session_id=owner.session_id,
                    requester_id=record.user_id,
                    model=model,
                    usage=usage,
                    initial_session=owner.initial_session,
                ),
            )

        return record_for_owner
    storage = binding.store
    if storage is None or not record.session_id:
        logger.warning("Mid-turn summary usage has no storage owner", run_id=record.run_id)
        return None
    return partial(
        record_summary_usage,
        storage=storage,
        session_id=record.session_id,
        requester_id=record.user_id,
        # The run's own session row may not exist yet while its first turn is still running.
        initial_session=new_scope_session(
            session_id=record.session_id,
            scope_id=binding.scope_id,
            is_team=binding.is_team,
        ),
    )


def _rewrite(messages: list[Message], layout: _Layout, summary_message: Message) -> None:
    """Replace the folded history and the turn's exchanges with the summary, keeping the kept messages in order.

    The summary follows the current prompt, where the turn's folded work was: a model that reads the prompt last can
    start the request over even when the summary records it finished. The prompt is marked so that a resume and later
    turns replay the scope's summary in the same place; a run-local summary is stored in place with its run.
    """
    leading = messages[: layout.leading]
    for message in leading:
        strip_embedded_summary(message)
    after_prompt = next((index + 1 for index, message in enumerate(layout.kept) if message is layout.prompt), 0)
    if after_prompt and is_scope_summary(summary_message):
        mark_summary_follows(layout.kept[after_prompt - 1])
    messages[:] = [*leading, *layout.kept[:after_prompt], summary_message, *layout.kept[after_prompt:]]


def _carry_request_usage(record: AnyRunRecord, folded: Sequence[Message]) -> None:
    """Keep the usage of folded requests in the run, since their messages leave it.

    Each request keeps its message id, so a run that still saves that message (a reply that failed afterwards) is not
    counted twice.
    """
    requests = [
        {**request, "message_id": message.id}
        for message in folded
        if message.role == "assistant"
        for request in project_requests([message.to_dict()]) or []
    ]
    if not requests:
        return
    metadata = dict(record.metadata or {})
    carried = metadata.get(COMPACTED_REQUESTS_METADATA_KEY)
    metadata[COMPACTED_REQUESTS_METADATA_KEY] = [*(carried if isinstance(carried, list) else []), *requests]
    record.metadata = metadata


__all__ = ["mid_turn_compaction_hook", "without_fold_records"]
