"""Scoped compaction."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from html import escape
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.summary import SessionSummary
from agno.utils.message import filter_tool_calls
from pydantic import BaseModel

from mindroom.constants import MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS, prompt_roles_for_history_storage
from mindroom.error_handling import is_model_safeguard_refusal
from mindroom.history.policy import persistable_summary_limit, summary_budget_is_admissible
from mindroom.history.storage import (
    compacted_run_ids_with,
    is_model_history_visible_run,
    record_compaction_chunk,
    remove_runs_by_id,
    seen_event_ids_for_runs,
    update_scope_seen_event_ids,
    update_scope_state_on_latest,
    write_scope_state,
)
from mindroom.history.summary_call import (
    DEFAULT_SUMMARY_RETRY_POLICY,
    CompactionSummaryOversizedOutputError,
    SummaryRetryPolicy,
    generate_compaction_summary,
    is_context_window_rejection,
)
from mindroom.history.types import (
    CarriedSummaryUnfitMarker,
    CompactionLifecycleProgress,
    CompactionOutcome,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistorySettings,
)
from mindroom.hooks import EVENT_COMPACTION_AFTER, EVENT_COMPACTION_BEFORE, CompactionHookContext, emit
from mindroom.logging_config import get_logger
from mindroom.model_instance_checks import is_genuine_openai_endpoint
from mindroom.timing import timed
from mindroom.token_budget import (
    compaction_estimate_kind,
    compaction_payload_token_upper_bound,
    estimate_text_tokens,
    stable_serialize,
)
from mindroom.tool_system.runtime_context import get_tool_runtime_context, resolve_tool_runtime_hook_bindings

if TYPE_CHECKING:
    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.history.summary_call import SummaryRetryDecision
    from mindroom.token_budget import CompactionEstimateKind

logger = get_logger(__name__)

_WRAPPER_OVERHEAD_TOKENS = 200
_OVERSIZED_RUN_NOTE = "Run truncated to fit compaction budget."
# Soft steering conversion between estimator units (bytes or tiktoken tokens)
# and prose words; only prompt wording depends on it, never the acceptance
# arithmetic.
_STEERING_ESTIMATOR_UNITS_PER_WORD = 5
_SUMMARY_METADATA_OMIT_KEYS = frozenset(
    {
        "model_params",
        "tools_schema",
    },
)


@dataclass(frozen=True)
class _ExcerptBlock:
    open_tag: str
    content: str
    close_tag: str

    def render(self, *, max_chars: int | None = None) -> str | None:
        snippet = self.content if max_chars is None else _truncate_excerpt(self.content, max_chars)
        if not snippet:
            return None
        return "\n".join([self.open_tag, _escape_xml_content(snippet), self.close_tag])


@dataclass(frozen=True)
class _CompactionRewriteResult:
    summary_text: str
    compacted_run_count: int
    compacted_run_ids: tuple[str, ...]
    compacted_messages: tuple[Message, ...]
    # The model that actually served the final persisted summary chunk; differs
    # from the configured primary after a safeguard-refusal fallback switch.
    summary_model: Model
    summary_model_name: str


@dataclass(frozen=True)
class _GeneratedSummaryChunk:
    summary: SessionSummary
    included_runs: list[RunOutput | TeamRunOutput]
    # The model that actually served this chunk (fallback after a refusal switch).
    model: Model
    model_name: str


def _persist_cleared_force_state_if_needed(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> HistoryScopeState:
    if not state.force_compact_before_next_run:
        return state
    return update_scope_state_on_latest(
        storage,
        session,
        scope,
        # Only clear when the durable row still matches the state this run read;
        # a concurrent write (for example a fresh manual request) wins otherwise.
        lambda latest: replace(latest, force_compact_before_next_run=False) if latest == state else latest,
    )


async def _emit_compaction_hook(
    *,
    event_name: str,
    scope: HistoryScope,
    messages: Sequence[Message],
    session_id: str,
    token_count_before: int,
    token_count_after: int | None,
    compaction_summary: str | None,
) -> None:
    runtime_context = get_tool_runtime_context()
    if runtime_context is None or not runtime_context.hook_registry.has_hooks(event_name):
        return

    bindings = resolve_tool_runtime_hook_bindings(runtime_context)
    correlation_id = runtime_context.correlation_id or f"{event_name}:{session_id}:{uuid4().hex}"
    context = CompactionHookContext(
        event_name=event_name,
        plugin_name="",
        settings={},
        config=runtime_context.config,
        runtime_paths=runtime_context.runtime_paths,
        logger=logger.bind(event_name=event_name, session_id=session_id),
        correlation_id=correlation_id,
        message_sender=bindings.message_sender,
        matrix_admin=bindings.matrix_admin,
        room_state_querier=bindings.room_state_querier,
        room_state_putter=bindings.room_state_putter,
        agent_name=scope.scope_id if scope.kind == "team" else runtime_context.agent_name,
        scope=scope,
        room_id=runtime_context.room_id,
        thread_id=runtime_context.resolved_thread_id,
        messages=list(messages),
        session_id=session_id,
        token_count_before=token_count_before,
        token_count_after=token_count_after,
        compaction_summary=compaction_summary,
    )
    await emit(runtime_context.hook_registry, event_name, context)


def _should_collect_compaction_hook_messages() -> bool:
    runtime_context = get_tool_runtime_context()
    if runtime_context is None:
        return False
    return runtime_context.hook_registry.has_hooks(EVENT_COMPACTION_BEFORE) or runtime_context.hook_registry.has_hooks(
        EVENT_COMPACTION_AFTER,
    )


@timed("system_prompt_assembly.history_prepare.compaction")
async def compact_scope_history(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
    summary_input_budget: int,
    summary_model: Model,
    summary_model_name: str,
    replay_window_tokens: int | None,
    threshold_tokens: int | None,
    summary_prompt: str,
    fallback_summary_model: Model | None = None,
    fallback_summary_model_name: str | None = None,
    fallback_summary_input_budget: int | None = None,
    lifecycle_notice_event_id: str | None = None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None = None,
) -> CompactionOutcome | None:
    """Compact one scope by rewriting session.summary and session.runs."""
    visible_runs = scope_visible_runs(session, scope)
    compactable_runs = _select_compaction_candidates(
        visible_runs=visible_runs,
        session=session,
        scope=scope,
        state=state,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
    )
    if not compactable_runs:
        _persist_cleared_force_state_if_needed(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
        )
        return None
    selected_run_ids = _stable_compaction_run_ids(
        compactable_runs,
        session_id=session.session_id,
        scope=scope,
    )
    if not selected_run_ids:
        _persist_cleared_force_state_if_needed(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
        )
        return None

    before_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    before_run_count = len(visible_runs)
    working_session = deepcopy(session)
    collect_compaction_hook_messages = _should_collect_compaction_hook_messages()

    async def emit_before_persist(included_runs: Sequence[RunOutput | TeamRunOutput]) -> None:
        await _emit_compaction_hook(
            event_name=EVENT_COMPACTION_BEFORE,
            scope=scope,
            messages=_messages_for_runs(included_runs, history_settings) if collect_compaction_hook_messages else (),
            session_id=session.session_id,
            token_count_before=before_tokens,
            token_count_after=None,
            compaction_summary=None,
        )

    rewrite_result = await _rewrite_working_session_for_compaction(
        storage=storage,
        persisted_session=session,
        working_session=working_session,
        summary_model=summary_model,
        summary_model_name=summary_model_name,
        fallback_summary_model=fallback_summary_model,
        fallback_summary_model_name=fallback_summary_model_name,
        fallback_summary_input_budget=fallback_summary_input_budget,
        session_id=session.session_id,
        scope=scope,
        state=state,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        selected_run_ids=selected_run_ids,
        summary_input_budget=summary_input_budget,
        before_tokens=before_tokens,
        runs_before=before_run_count,
        threshold_tokens=threshold_tokens,
        summary_prompt=summary_prompt,
        lifecycle_notice_event_id=lifecycle_notice_event_id,
        progress_callback=progress_callback,
        collect_compaction_hook_messages=collect_compaction_hook_messages,
        before_persist_callback=emit_before_persist,
    )
    if rewrite_result is None:
        _persist_cleared_force_state_if_needed(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
        )
        return None

    compacted_at = _iso_utc_now()
    new_state = HistoryScopeState(
        last_compacted_at=compacted_at,
        last_summary_model=_model_identifier(rewrite_result.summary_model),
        last_compacted_run_count=rewrite_result.compacted_run_count,
        compacted_run_ids=compacted_run_ids_with(state, rewrite_result.compacted_run_ids),
        force_compact_before_next_run=False,
    )
    write_scope_state(session, scope, new_state)
    write_scope_state(working_session, scope, new_state)
    record_compaction_chunk(
        storage=storage,
        persisted_session=session,
        working_session=working_session,
        scope=scope,
        compacted_run_ids=rewrite_result.compacted_run_ids,
        sync_remaining_runs=True,
    )
    logger.info(
        "Compaction summary generated",
        session_id=session.session_id,
        scope=scope.key,
        compacted_runs=rewrite_result.compacted_run_count,
        model=_model_identifier(rewrite_result.summary_model),
    )

    after_visible_runs = scope_visible_runs(session, scope)
    after_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    outcome = CompactionOutcome(
        mode="manual" if state.force_compact_before_next_run else "auto",
        session_id=session.session_id,
        scope=scope.key,
        summary=rewrite_result.summary_text,
        summary_model=rewrite_result.summary_model_name,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        window_tokens=replay_window_tokens or 0,
        threshold_tokens=threshold_tokens or 0,
        runs_before=before_run_count,
        runs_after=len(after_visible_runs),
        compacted_run_count=rewrite_result.compacted_run_count,
        compacted_at=compacted_at,
        history_budget_tokens=available_history_budget,
    )
    await _emit_compaction_hook(
        event_name=EVENT_COMPACTION_AFTER,
        scope=scope,
        messages=rewrite_result.compacted_messages,
        session_id=session.session_id,
        token_count_before=before_tokens,
        token_count_after=after_tokens,
        compaction_summary=rewrite_result.summary_text,
    )
    return outcome


@timed("system_prompt_assembly.history_prepare.compaction.rewrite_working_session")
async def _rewrite_working_session_for_compaction(  # noqa: C901, PLR0912, PLR0915
    *,
    storage: BaseDb,
    persisted_session: AgentSession | TeamSession,
    working_session: AgentSession | TeamSession,
    summary_model: Model,
    summary_model_name: str,
    session_id: str,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
    selected_run_ids: Sequence[str],
    summary_input_budget: int,
    before_tokens: int,
    runs_before: int,
    threshold_tokens: int | None,
    lifecycle_notice_event_id: str | None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    collect_compaction_hook_messages: bool,
    summary_prompt: str,
    fallback_summary_model: Model | None = None,
    fallback_summary_model_name: str | None = None,
    fallback_summary_input_budget: int | None = None,
    before_persist_callback: Callable[[Sequence[RunOutput | TeamRunOutput]], Awaitable[None]] | None = None,
) -> _CompactionRewriteResult | None:
    final_summary_text = _current_summary_text(working_session) or ""
    sizing = _resolve_compaction_sizing_context(summary_model, summary_model_name, summary_input_budget)
    fallback_sizing: _CompactionSizingContext | None = None
    if fallback_summary_model is not None:
        assert fallback_summary_model_name is not None
        # An admitted fallback always carries its own resolved budget: the
        # runtime owner excludes a fallback whose plan is unavailable before
        # this seam, so it can never serve requests its own plan rejects.
        assert fallback_summary_input_budget is not None
        fallback_sizing = _resolve_compaction_sizing_context(
            fallback_summary_model,
            fallback_summary_model_name,
            fallback_summary_input_budget,
        )
    # I1 must hold for every profile that can serve the next compaction
    # attempt: the configured primary serves the next turn even after a
    # mid-rewrite fallback switch, so this set never shrinks during a rewrite.
    acceptance_contexts = (sizing,) if fallback_sizing is None else (sizing, fallback_sizing)
    total_compacted_run_count = 0
    all_compacted_run_ids: list[str] = []
    all_compacted_run_id_set: set[str] = set()
    compacted_messages: list[Message] = []
    pending_selected_run_ids = set(selected_run_ids)

    while pending_selected_run_ids:
        working_visible_runs = scope_visible_runs(working_session, scope)
        compactable_runs = [
            run
            for run in working_visible_runs
            if isinstance(run.run_id, str) and run.run_id in pending_selected_run_ids
        ]
        if not compactable_runs:
            break

        previous_summary = _current_summary_text(working_session)
        # Every build, retry, log, and backstop decision is sized by the
        # ACTIVE serving context's own budget; only the immediate post-refusal
        # fallback resend inside the retry wrapper keeps its already-built
        # primary-sized bytes (the unchanged-input contract). The acceptance
        # check's proof domain is the planner-admissible budget range: below
        # the availability floor the degenerate-budget no-op contract applies.
        enforce_acceptance = summary_budget_is_admissible(sizing.summary_input_budget)
        summary_input, included_runs = _build_summary_input(
            previous_summary=previous_summary,
            compacted_runs=compactable_runs,
            history_settings=history_settings,
            max_input_tokens=sizing.summary_input_budget,
            token_estimator=sizing.token_estimator,
        )
        if not included_runs and previous_summary is not None and enforce_acceptance:
            # The acceptance check (I1) makes this corner unreachable from
            # summaries this loop persists, so only an inherited stored
            # summary — written before the check existed, or under a larger
            # model/budget — lands here. Condense the COMPLETE summary in its
            # own provider-arbitrated request; every terminal verdict is
            # durable (persisted summary-only progress or a one-shot unfit
            # marker), so the attempt is never silently re-bought.
            condensation = await _condense_carried_summary(
                storage=storage,
                persisted_session=persisted_session,
                working_session=working_session,
                sizing=sizing,
                fallback_sizing=fallback_sizing,
                acceptance_contexts=acceptance_contexts,
                previous_summary=previous_summary,
                session_id=session_id,
                scope=scope,
                state=state,
                history_settings=history_settings,
                summary_prompt=summary_prompt,
            )
            if condensation.serving is not sizing:
                # The fallback served the condensation; exactly as after a
                # fallback-served chunk, it becomes the active serving
                # context for everything that follows.
                sizing = condensation.serving
                fallback_sizing = None
                enforce_acceptance = summary_budget_is_admissible(sizing.summary_input_budget)
            if condensation.persisted_summary is not None:
                previous_summary = condensation.persisted_summary
                final_summary_text = condensation.persisted_summary
                await _emit_lifecycle_progress_after_persist(
                    working_session=working_session,
                    scope=scope,
                    state=state,
                    history_settings=history_settings,
                    lifecycle_notice_event_id=lifecycle_notice_event_id,
                    progress_callback=progress_callback,
                    session_id=session_id,
                    summary_model_name=sizing.model_name,
                    before_tokens=before_tokens,
                    available_history_budget=available_history_budget,
                    runs_before=runs_before,
                    threshold_tokens=threshold_tokens,
                    total_compacted_run_count=total_compacted_run_count,
                    selected_runs_remaining=len(pending_selected_run_ids),
                )
                summary_input, included_runs = _build_summary_input(
                    previous_summary=previous_summary,
                    compacted_runs=compactable_runs,
                    history_settings=history_settings,
                    max_input_tokens=sizing.summary_input_budget,
                    token_estimator=sizing.token_estimator,
                )
        if not included_runs:
            logger.warning(
                "Compaction skipped because no run fit the single-pass summary budget",
                session_id=session_id,
                scope=scope.key,
                candidate_runs=len(compactable_runs),
                summary_input_budget_tokens=sizing.summary_input_budget,
            )
            if total_compacted_run_count == 0:
                return None
            break

        new_summary = await _generate_compaction_summary_with_retry(
            sizing=sizing,
            acceptance_contexts=acceptance_contexts,
            enforce_acceptance=enforce_acceptance,
            previous_summary=previous_summary,
            compactable_runs=compactable_runs,
            initial_summary_input=summary_input,
            initial_included_runs=included_runs,
            summary_input_budget=sizing.summary_input_budget,
            session_id=session_id,
            scope=scope,
            history_settings=history_settings,
            summary_prompt=summary_prompt,
            fallback_sizing=fallback_sizing,
        )
        if new_summary.model is not sizing.model:
            # A safeguard-refusal fallback served this chunk; its sizing
            # context serves every later chunk — token estimation, logged
            # estimate kind, and acceptance limit switch together. Acceptance
            # still covers the configured primary, which serves the next
            # turn's attempt.
            assert fallback_sizing is not None
            sizing = fallback_sizing
            fallback_sizing = None
        included_runs = new_summary.included_runs
        generated_summary = new_summary.summary
        if before_persist_callback is not None:
            await before_persist_callback(included_runs)
        final_summary_text = generated_summary.summary
        compacted_run_ids = tuple(run.run_id for run in included_runs if isinstance(run.run_id, str) and run.run_id)
        compacted_seen_event_ids = sorted(seen_event_ids_for_runs(included_runs))
        working_session.summary = SessionSummary(summary=generated_summary.summary, updated_at=datetime.now(UTC))
        if compacted_seen_event_ids:
            update_scope_seen_event_ids(working_session, scope, compacted_seen_event_ids)
        working_session.runs = remove_runs_by_id(working_session.runs or [], compacted_run_ids)
        total_compacted_run_count += len(included_runs)
        for run_id in compacted_run_ids:
            if run_id not in all_compacted_run_id_set:
                all_compacted_run_id_set.add(run_id)
                all_compacted_run_ids.append(run_id)
        if collect_compaction_hook_messages:
            compacted_messages.extend(_messages_for_runs(included_runs, history_settings))
        pending_selected_run_ids.difference_update(compacted_run_ids)

        record_compaction_chunk(
            storage=storage,
            persisted_session=persisted_session,
            working_session=working_session,
            scope=scope,
            compacted_run_ids=compacted_run_ids,
        )

        await _emit_lifecycle_progress_after_persist(
            working_session=working_session,
            scope=scope,
            state=state,
            history_settings=history_settings,
            lifecycle_notice_event_id=lifecycle_notice_event_id,
            progress_callback=progress_callback,
            session_id=session_id,
            summary_model_name=sizing.model_name,
            before_tokens=before_tokens,
            available_history_budget=available_history_budget,
            runs_before=runs_before,
            threshold_tokens=threshold_tokens,
            total_compacted_run_count=total_compacted_run_count,
            selected_runs_remaining=len(pending_selected_run_ids),
        )

    if total_compacted_run_count == 0:
        return None
    for run in scope_visible_runs(working_session, scope):
        _strip_stale_anthropic_replay_fields(run.messages or [])
    return _CompactionRewriteResult(
        summary_text=final_summary_text,
        compacted_run_count=total_compacted_run_count,
        compacted_run_ids=tuple(all_compacted_run_ids),
        compacted_messages=tuple(compacted_messages),
        summary_model=sizing.model,
        summary_model_name=sizing.model_name,
    )


@dataclass(frozen=True)
class _CompactionSizingContext:
    """Sizing facts for one model profile that can serve a compaction attempt.

    The estimator, its logged kind, and the persisted-summary acceptance limit
    resolve together from one endpoint-trust evaluation, so the logged kind
    provably describes the arithmetic used for every request this profile
    receives even if the environment changes mid-compaction. The rewrite
    re-resolves the serving context at the safeguard-fallback switch seam, so
    the frozen-labels property holds per serving model instead of per rewrite.
    """

    model: Model
    model_name: str
    genuine_openai_endpoint: bool
    token_estimator: Callable[[str], int]
    estimate_kind: CompactionEstimateKind
    summary_input_budget: int
    acceptance_limit: int
    # Stable, non-secret identity of the serving profile: provider/model
    # class, model id, endpoint-trust classification, and estimator kind.
    # The unfit marker keys on it, so a provider or endpoint switch that
    # keeps the bare model id re-enables one fresh condensation attempt.
    serving_profile: str


def _serving_profile_fingerprint(
    summary_model: Model,
    *,
    genuine_openai_endpoint: bool,
    estimate_kind: CompactionEstimateKind,
) -> str:
    """Return the stable serving-profile identity for one resolved sizing context."""
    model_class = type(summary_model)
    return (
        f"class={model_class.__module__}.{model_class.__qualname__}"
        f"|provider={summary_model.provider or ''}"
        f"|id={summary_model.id or ''}"
        f"|genuine_openai_endpoint={genuine_openai_endpoint}"
        f"|estimate_kind={estimate_kind}"
    )


def _resolve_compaction_sizing_context(
    summary_model: Model,
    summary_model_name: str,
    summary_input_budget: int,
) -> _CompactionSizingContext:
    """Resolve the sizing context for one loaded summary model and input budget."""
    genuine_openai_endpoint = is_genuine_openai_endpoint(summary_model)
    estimate_kind = compaction_estimate_kind(summary_model.id, genuine_openai_endpoint=genuine_openai_endpoint)
    return _CompactionSizingContext(
        model=summary_model,
        model_name=summary_model_name,
        genuine_openai_endpoint=genuine_openai_endpoint,
        token_estimator=partial(
            compaction_payload_token_upper_bound,
            model_id=summary_model.id,
            genuine_openai_endpoint=genuine_openai_endpoint,
        ),
        estimate_kind=estimate_kind,
        summary_input_budget=summary_input_budget,
        acceptance_limit=persistable_summary_limit(summary_input_budget),
        serving_profile=_serving_profile_fingerprint(
            summary_model,
            genuine_openai_endpoint=genuine_openai_endpoint,
            estimate_kind=estimate_kind,
        ),
    )


async def _emit_lifecycle_progress_after_persist(
    *,
    working_session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    lifecycle_notice_event_id: str | None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    session_id: str,
    summary_model_name: str,
    before_tokens: int,
    available_history_budget: int | None,
    runs_before: int,
    threshold_tokens: int | None,
    total_compacted_run_count: int,
    selected_runs_remaining: int,
) -> None:
    """Emit lifecycle progress after a compaction chunk has been durably persisted."""
    remaining_runs = scope_visible_runs(working_session, scope)
    if progress_callback is None or not remaining_runs:
        return
    after_tokens = estimate_prompt_visible_history_tokens(
        session=working_session,
        scope=scope,
        history_settings=history_settings,
    )
    await progress_callback(
        CompactionLifecycleProgress(
            notice_event_id=lifecycle_notice_event_id,
            mode="manual" if state.force_compact_before_next_run else "auto",
            session_id=session_id,
            scope=scope.key,
            summary_model=summary_model_name,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            history_budget_tokens=available_history_budget,
            runs_before=runs_before,
            compacted_run_count=total_compacted_run_count,
            runs_remaining=selected_runs_remaining,
            threshold_tokens=threshold_tokens,
        ),
    )


def _sizing_log_fields(*, kind: CompactionEstimateKind, estimate: int, budget_tokens: int) -> dict[str, object]:
    """Truthful sizing fields shared by the compaction chunk log events.

    ``kind`` is resolved once next to the estimator partial, so the logged
    kind provably describes the arithmetic the frozen estimator used.
    ``summary_input_budget_tokens`` is denominated in the same units as the
    estimate — shrink retries derive the next budget from the estimate — so
    ``summary_input_estimate_kind`` disambiguates both fields.
    """
    return {
        "summary_input_estimate": estimate,
        "summary_input_estimate_kind": kind,
        "summary_input_budget_tokens": budget_tokens,
    }


def _acceptance_violation(
    summary_text: str,
    acceptance_contexts: tuple[_CompactionSizingContext, ...],
) -> tuple[_CompactionSizingContext, int] | None:
    """Return the first profile whose acceptance limit the candidate exceeds, with its estimate.

    Evaluates the exact pure function the next turn's builder evaluates — the
    profile's estimator over the escaped, wrapped previous-summary block — so
    there is no estimation gap between what is persisted and what must fit.
    """
    summary_block = _previous_summary_block(summary_text)
    for context in acceptance_contexts:
        estimate = context.token_estimator(summary_block)
        if estimate > context.acceptance_limit:
            return context, estimate
    return None


def _require_acceptable_summary(
    summary_text: str,
    acceptance_contexts: tuple[_CompactionSizingContext, ...],
) -> None:
    """Enforce the persisted-summary fit invariant (I1) on one candidate."""
    violation = _acceptance_violation(summary_text, acceptance_contexts)
    if violation is None:
        return
    context, estimate = violation
    msg = (
        f"generated summary block sizes at {estimate} {context.estimate_kind} units for "
        f"model {context.model_name}, above the {context.acceptance_limit}-unit persistable-summary "
        f"limit of its {context.summary_input_budget}-unit input budget"
    )
    raise CompactionSummaryOversizedOutputError(msg)


def _steer_summary_prompt(
    summary_prompt: str,
    acceptance_contexts: tuple[_CompactionSizingContext, ...],
) -> str:
    """Append a soft word target derived from the tightest acceptance limit.

    Steering only, so the acceptance check almost never fires; the check is
    the guarantee. Provider ``max_tokens`` is deliberately untouched: no
    client-known bound converts output tokens to estimator units, and lowering
    it would turn legitimately long merges into output-limit retry loops.
    """
    if not acceptance_contexts:
        return summary_prompt
    word_target = min(context.acceptance_limit for context in acceptance_contexts) // _STEERING_ESTIMATOR_UNITS_PER_WORD
    if word_target <= 0:
        return summary_prompt
    return (
        f"{summary_prompt}\n"
        f"Keep the merged summary under roughly {word_target:,} words. "
        "The target is soft: never drop a still-relevant fact to meet it."
    )


async def _generate_compaction_summary_with_retry(
    *,
    sizing: _CompactionSizingContext,
    acceptance_contexts: tuple[_CompactionSizingContext, ...],
    previous_summary: str | None,
    compactable_runs: Sequence[RunOutput | TeamRunOutput],
    initial_summary_input: str,
    initial_included_runs: list[RunOutput | TeamRunOutput],
    summary_input_budget: int,
    session_id: str,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    summary_prompt: str,
    retry_policy: SummaryRetryPolicy = DEFAULT_SUMMARY_RETRY_POLICY,
    enforce_acceptance: bool = True,
    fallback_sizing: _CompactionSizingContext | None = None,
) -> _GeneratedSummaryChunk:
    """Generate one summary chunk, retrying the same or smaller input when safe.

    With ``enforce_acceptance`` (every chunk merge), a returned candidate must
    satisfy the persisted-summary fit invariant for every profile in
    ``acceptance_contexts``; a failing candidate raises the typed shrinkable
    ``CompactionSummaryOversizedOutputError`` so the policy shrinks the input
    and retries once, and exhaustion propagates with nothing persisted. The
    condensation backstop disables enforcement and categorizes the candidate
    itself, because a strictly smaller but still unfit output is durable
    progress there, not a failure.

    A safeguard refusal from the primary model switches once to
    ``fallback_sizing``, keeping the ``summary_prompt`` and ``summary_input``
    bytes, included runs, and budget unchanged (only the target model and its
    sizing labels differ); a refusal or failure from the fallback propagates.
    The switch shares the retry policy's attempt bound, so a refusal after an
    earlier shrink or transient retry propagates without a fallback call. All
    other failures keep the existing shrink and transient same-input retry
    behavior.
    """
    summary_input = initial_summary_input
    included_runs = initial_included_runs
    budget = summary_input_budget
    steered_summary_prompt = _steer_summary_prompt(summary_prompt, acceptance_contexts)
    minimum_progress_input_tokens = (
        _minimum_progress_input_tokens(
            previous_summary=previous_summary,
            first_run=compactable_runs[0],
            token_estimator=sizing.token_estimator,
        )
        if compactable_runs and retry_policy.shrink_allowed
        else 0
    )
    attempt = 1
    while True:
        estimated_input_tokens = sizing.token_estimator(summary_input)
        started = asyncio.get_running_loop().time()
        logger.info(
            "Compaction summary chunk request",
            session_id=session_id,
            scope=scope.key,
            model_name=sizing.model_name,
            attempt=attempt,
            candidate_runs=len(compactable_runs),
            included_runs=len(included_runs),
            **_sizing_log_fields(kind=sizing.estimate_kind, estimate=estimated_input_tokens, budget_tokens=budget),
            timeout_seconds=MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
        )
        try:
            summary = await generate_compaction_summary(
                model=sizing.model,
                summary_input=summary_input,
                summary_prompt=steered_summary_prompt,
            )
            if enforce_acceptance:
                _require_acceptable_summary(summary.summary, acceptance_contexts)
        except Exception as exc:
            duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            logger.warning(
                "Compaction summary chunk failed",
                session_id=session_id,
                scope=scope.key,
                model_name=sizing.model_name,
                attempt=attempt,
                candidate_runs=len(compactable_runs),
                included_runs=len(included_runs),
                **_sizing_log_fields(kind=sizing.estimate_kind, estimate=estimated_input_tokens, budget_tokens=budget),
                timeout_seconds=MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
                duration_ms=duration_ms,
                error=str(exc) or type(exc).__name__,
            )
            # The attempt bound covers the fallback call too: a refusal after an
            # earlier shrink or transient retry propagates instead of issuing a
            # third provider call.
            if fallback_sizing is not None and attempt < retry_policy.max_attempts and is_model_safeguard_refusal(exc):
                logger.info(
                    "Compaction summary refused; switching to fallback model",
                    session_id=session_id,
                    scope=scope.key,
                    attempt=attempt,
                    refused_model=sizing.model_name,
                    fallback_model=fallback_sizing.model_name,
                )
                # The fallback resends the unchanged prompt and input bytes
                # exactly once; only the target model and its sizing labels
                # differ.
                sizing = fallback_sizing
                fallback_sizing = None
                attempt += 1
                continue
            retry_decision: SummaryRetryDecision | None = retry_policy.retry_budget(
                attempt=attempt,
                budget=budget,
                input_tokens=estimated_input_tokens,
                minimum_progress_input_tokens=minimum_progress_input_tokens,
                error=exc,
            )
            if retry_decision is not None:
                if retry_decision.kind == "same-budget-transient":
                    await asyncio.sleep(retry_policy.same_input_retry_delay_seconds)
                    attempt += 1
                    continue
                rebuilt_input, rebuilt_runs = _build_summary_input(
                    previous_summary=previous_summary,
                    compacted_runs=compactable_runs,
                    history_settings=history_settings,
                    max_input_tokens=retry_decision.budget,
                    token_estimator=sizing.token_estimator,
                )
                if rebuilt_runs:
                    rebuilt_input_tokens = sizing.token_estimator(rebuilt_input)
                    if retry_decision.kind == "shrink" and rebuilt_input_tokens >= estimated_input_tokens:
                        raise
                    summary_input = rebuilt_input
                    included_runs = rebuilt_runs
                    budget = retry_decision.budget
                    attempt += 1
                    continue
            raise
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        logger.info(
            "Compaction summary chunk completed",
            session_id=session_id,
            scope=scope.key,
            model_name=sizing.model_name,
            attempt=attempt,
            candidate_runs=len(compactable_runs),
            included_runs=len(included_runs),
            **_sizing_log_fields(kind=sizing.estimate_kind, estimate=estimated_input_tokens, budget_tokens=budget),
            timeout_seconds=MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
            duration_ms=duration_ms,
        )
        return _GeneratedSummaryChunk(
            summary=summary,
            included_runs=included_runs,
            model=sizing.model,
            model_name=sizing.model_name,
        )


@timed("system_prompt_assembly.history_prepare.compaction.summary_input_build")
def _build_summary_input(
    *,
    previous_summary: str | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    max_input_tokens: int,
    history_settings: ResolvedHistorySettings,
    token_estimator: Callable[[str], int],
) -> tuple[str, list[RunOutput | TeamRunOutput]]:
    summary_block = ""
    if previous_summary is not None and previous_summary.strip():
        summary_block = _previous_summary_block(previous_summary)

    empty_input = _compose_summary_input(summary_block, "")
    remaining = max_input_tokens - token_estimator(empty_input) - _WRAPPER_OVERHEAD_TOKENS

    if remaining <= 0:
        return _build_oversized_summary_input(
            previous_summary=previous_summary,
            compacted_runs=compacted_runs[:1],
            history_settings=history_settings,
            max_input_tokens=max_input_tokens,
            token_estimator=token_estimator,
        )

    included_runs: list[RunOutput | TeamRunOutput] = []
    serialized_runs: list[str] = []
    for index, run in enumerate(compacted_runs):
        serialized_run = _serialize_run(run, index, history_settings)
        separator = "\n\n" if serialized_runs else ""
        run_tokens = token_estimator(f"{separator}{serialized_run}")
        if run_tokens > remaining:
            if not included_runs:
                return _build_oversized_summary_input(
                    previous_summary=previous_summary,
                    compacted_runs=[run],
                    history_settings=history_settings,
                    max_input_tokens=max_input_tokens,
                    token_estimator=token_estimator,
                )
            break
        included_runs.append(run)
        serialized_runs.append(serialized_run)
        remaining -= run_tokens

    if not included_runs:
        return summary_block, []

    return _compose_summary_input(summary_block, "\n\n".join(serialized_runs)), included_runs


def _build_oversized_summary_input(
    *,
    previous_summary: str | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    history_settings: ResolvedHistorySettings,
    max_input_tokens: int,
    token_estimator: Callable[[str], int],
) -> tuple[str, list[RunOutput | TeamRunOutput]]:
    summary_block = (
        _previous_summary_block(previous_summary) if previous_summary is not None and previous_summary.strip() else ""
    )
    if not compacted_runs:
        return summary_block, []
    first_run = compacted_runs[0]
    oversized_excerpt = _serialize_oversized_run_excerpt(
        first_run,
        index=0,
        history_settings=history_settings,
        max_tokens=_remaining_excerpt_budget(max_input_tokens, summary_block, token_estimator),
        token_estimator=token_estimator,
    )
    if oversized_excerpt is None:
        return summary_block, []
    return _compose_summary_input(summary_block, oversized_excerpt), [first_run]


def _minimum_progress_input_tokens(
    *,
    previous_summary: str | None,
    first_run: RunOutput | TeamRunOutput,
    token_estimator: Callable[[str], int],
) -> int:
    """Return the smallest shrink budget preserving the prior summary and one run envelope.

    Below this size ``_build_summary_input`` rebuilds to a run-less input
    because the previous-summary block alone swallows the envelope, so
    ``SummaryRetryPolicy`` clamps shrink targets here. A zero content budget
    renders the run as its open tag, truncation note, and close tag; the
    wrapper overhead covers the builder's own envelope accounting and
    tokenizer boundary effects.
    """
    summary_block = (
        _previous_summary_block(previous_summary) if previous_summary is not None and previous_summary.strip() else ""
    )
    minimal_excerpt = _serialize_run_excerpt(first_run, index=0, blocks=(), content_budget_chars=0)
    return token_estimator(_compose_summary_input(summary_block, minimal_excerpt)) + _WRAPPER_OVERHEAD_TOKENS


def _serialize_oversized_run_excerpt(
    run: RunOutput | TeamRunOutput,
    *,
    index: int,
    history_settings: ResolvedHistorySettings,
    max_tokens: int,
    token_estimator: Callable[[str], int],
) -> str | None:
    if max_tokens <= 0:
        return None

    full_run = _serialize_run(run, index, history_settings)
    if token_estimator(full_run) <= max_tokens:
        return full_run

    blocks = _excerpt_blocks(run, history_settings)
    budget_chars = max_tokens * 4
    while budget_chars > 0:
        excerpt = _serialize_run_excerpt(run, index=index, blocks=blocks, content_budget_chars=budget_chars)
        if token_estimator(excerpt) <= max_tokens:
            return excerpt
        budget_chars //= 2

    minimal_excerpt = _serialize_run_excerpt(run, index=index, blocks=blocks, content_budget_chars=0)
    if token_estimator(minimal_excerpt) <= max_tokens:
        return minimal_excerpt
    return None


def _serialize_run_excerpt(
    run: RunOutput | TeamRunOutput,
    *,
    index: int,
    blocks: Sequence[_ExcerptBlock],
    content_budget_chars: int,
) -> str:
    lines = [_run_open_tag(run, index), f"<note>{_OVERSIZED_RUN_NOTE}</note>"]
    remaining_chars = content_budget_chars
    for block in blocks:
        if remaining_chars <= 0:
            break
        rendered = block.render(max_chars=remaining_chars)
        if rendered is None:
            continue
        lines.append(rendered)
        if len(block.content) <= remaining_chars:
            remaining_chars -= len(block.content)
        else:
            break

    lines.append("</run>")
    return "\n".join(lines)


def _compaction_replay_messages(
    run: RunOutput | TeamRunOutput,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    skip_roles = set(_history_skip_roles(history_settings))
    messages = [deepcopy(message) for message in run.messages or [] if message.role not in skip_roles]
    if history_settings.max_tool_calls_from_history is not None and messages:
        filter_tool_calls(messages, history_settings.max_tool_calls_from_history)
    _strip_stale_anthropic_replay_fields(messages)
    return messages


def _excerpt_blocks(run: RunOutput | TeamRunOutput, history_settings: ResolvedHistorySettings) -> list[_ExcerptBlock]:
    blocks: list[_ExcerptBlock] = []
    if run.metadata:
        metadata = _metadata_for_summary(run.metadata)
        if metadata:
            blocks.append(_ExcerptBlock("<run_metadata>", stable_serialize(metadata), "</run_metadata>"))
    for message in _compaction_replay_messages(run, history_settings):
        content = _render_message_content(message)
        if not content:
            continue
        blocks.append(_ExcerptBlock(_message_open_tag(message), content, "</message>"))
    return blocks


def _metadata_for_summary(metadata: dict[str, object]) -> dict[str, object]:
    """Omit bulky request metadata from compaction summary inputs."""
    return {key: value for key, value in metadata.items() if key not in _SUMMARY_METADATA_OMIT_KEYS}


def _truncate_excerpt(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return f"{text[: max_chars - 1].rstrip()}…"


def _remaining_excerpt_budget(
    max_input_tokens: int,
    summary_block: str,
    token_estimator: Callable[[str], int],
) -> int:
    return max_input_tokens - token_estimator(_compose_summary_input(summary_block, ""))


def _compose_summary_input(summary_block: str, serialized_runs: str) -> str:
    parts: list[str] = []
    if summary_block:
        parts.append(summary_block)
    parts.append(f"<new_conversation>\n{serialized_runs}\n</new_conversation>")
    return "\n\n".join(parts)


class _CarriedSummaryUnfitError(RuntimeError):
    """Terminal condensation failure: the provider verified the summary cannot fit its window."""


# The backstop input is complete-summary-or-nothing (E1), so shrink is
# structurally impossible; transient failures keep the standard one delayed
# same-budget retry. The strengthened retry disables even that so the corner's
# provider spend stays bounded per distinct (summary, model, budget) state.
_CONDENSATION_RETRY_POLICY = SummaryRetryPolicy(shrink_allowed=False)
_CONDENSATION_STRENGTHENED_RETRY_POLICY = SummaryRetryPolicy(max_attempts=1, shrink_allowed=False)


def _condense_note(*, word_target: int | None) -> str:
    """Build the loss-aware condensation instruction, optionally with the numeric target."""
    base = (
        "The previous summary alone exceeds the compaction input budget, "
        "so there are no new runs in this pass. Rewrite the previous summary "
        "more concisely in the required structure, preserving every "
        "still-relevant fact, especially Next Steps and Critical Context."
    )
    if word_target is None:
        return f"<note>{base}</note>"
    return f"<note>{base} The rewritten summary MUST stay under {word_target:,} words.</note>"


def _summary_digest(summary_text: str) -> str:
    """Return the stable identity of one stored summary text for the unfit marker."""
    return hashlib.sha256(summary_text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _persist_summary_only_chunk(
    *,
    storage: BaseDb,
    persisted_session: AgentSession | TeamSession,
    working_session: AgentSession | TeamSession,
    scope: HistoryScope,
    summary_text: str,
) -> None:
    """Durably persist condensed-summary progress with no run removals (I5).

    A summary-only chunk is a complete, E1-valid state transition: runs and
    tombstones stay untouched, so successful condensation work is never
    generated-and-discarded and never re-bought.
    """
    working_session.summary = SessionSummary(summary=summary_text, updated_at=datetime.now(UTC))
    record_compaction_chunk(
        storage=storage,
        persisted_session=persisted_session,
        working_session=working_session,
        scope=scope,
        compacted_run_ids=(),
    )


def _record_carried_summary_unfit(
    *,
    storage: BaseDb,
    persisted_session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    marker: CarriedSummaryUnfitMarker,
) -> None:
    """Durably write the one-shot unfit marker (control state only; I3, I4).

    Consumes only the force request that started this attempt: force writes
    bump ``force_compact_generation``, so the flag is cleared only while the
    persisted generation still equals the one this attempt read. A fresh
    request recorded while the provider call was in flight carries a newer
    generation and survives the clear.
    """

    def _with_marker(latest: HistoryScopeState) -> HistoryScopeState:
        consumed_force = (
            state.force_compact_before_next_run and latest.force_compact_generation == state.force_compact_generation
        )
        return replace(
            latest,
            carried_summary_unfit=marker,
            force_compact_before_next_run=latest.force_compact_before_next_run and not consumed_force,
        )

    update_scope_state_on_latest(storage, persisted_session, scope, _with_marker)


@dataclass(frozen=True)
class _CondensationOutcome:
    """Result of one condensation backstop attempt.

    ``serving`` is the sizing context that actually served the attempt — the
    fallback after a safeguard-refusal switch — so the outer rewrite can adopt
    it for later chunks, audit state, and the outcome, exactly as after a
    fallback-served chunk. ``persisted_summary`` is the durably persisted
    condensed text, or None when nothing was persisted.
    """

    persisted_summary: str | None
    serving: _CompactionSizingContext


async def _condense_carried_summary(  # noqa: C901
    *,
    storage: BaseDb,
    persisted_session: AgentSession | TeamSession,
    working_session: AgentSession | TeamSession,
    sizing: _CompactionSizingContext,
    fallback_sizing: _CompactionSizingContext | None,
    acceptance_contexts: tuple[_CompactionSizingContext, ...],
    previous_summary: str,
    session_id: str,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    summary_prompt: str,
) -> _CondensationOutcome:
    """Condense a complete carried summary that left no room for any run.

    There is deliberately no client-side send gate: the provider is the only
    accurate arbiter of whether the request fits, so neither the conservative
    false-negative stall nor an approximate over-context guess can occur here.
    The model always receives the COMPLETE previous summary (E1); acceptance
    gates only what is persisted. A safeguard refusal switches once to the
    configured fallback with the request bytes unchanged, and the fallback
    then serves the rest of the backstop. Every terminal verdict is durable:
    a fitting or strictly smaller output persists immediately as a
    summary-only chunk, and a failure verdict writes a one-shot marker keyed
    on (summary digest, serving profile, budget) so it is never automatically
    re-purchased for the same state.
    """
    stored_digest = _summary_digest(previous_summary)
    marker = state.carried_summary_unfit

    def _marker_matches(profile: _CompactionSizingContext) -> bool:
        return (
            marker is not None
            and marker.summary_digest == stored_digest
            and marker.serving_profile == profile.serving_profile
            and marker.summary_input_budget == profile.summary_input_budget
        )

    # The marker suppresses the attempt when it matches ANY profile that
    # could serve this attempt-set: after a refusal-switched terminal verdict
    # the marker carries the fallback's profile, and re-running the set would
    # re-purchase the primary's refusal plus the fallback's known verdict.
    if not state.force_compact_before_next_run and (
        _marker_matches(sizing) or (fallback_sizing is not None and _marker_matches(fallback_sizing))
    ):
        assert marker is not None
        logger.info(
            "Compaction condensation skipped by carried-summary unfit marker",
            session_id=session_id,
            scope=scope.key,
            marker_reason=marker.reason,
            marker_failed_at=marker.failed_at,
            marker_serving_profile=marker.serving_profile,
            summary_input_budget_tokens=marker.summary_input_budget,
        )
        return _CondensationOutcome(persisted_summary=None, serving=sizing)

    serving = sizing
    remaining_fallback = fallback_sizing

    def _block_size(summary_text: str) -> int:
        return serving.token_estimator(_previous_summary_block(summary_text))

    def _mark_unfit(summary_digest: str, reason: str) -> None:
        _record_carried_summary_unfit(
            storage=storage,
            persisted_session=persisted_session,
            scope=scope,
            state=state,
            marker=CarriedSummaryUnfitMarker(
                summary_digest=summary_digest,
                serving_profile=serving.serving_profile,
                summary_input_budget=serving.summary_input_budget,
                failed_at=_iso_utc_now(),
                reason=reason,
            ),
        )

    async def _condense_once(*, word_target: int | None, retry_policy: SummaryRetryPolicy) -> SessionSummary:
        nonlocal serving, remaining_fallback
        condensation_input = _compose_summary_input(
            _previous_summary_block(previous_summary),
            _condense_note(word_target=word_target),
        )
        logger.info(
            "Compaction condensing carried summary",
            session_id=session_id,
            scope=scope.key,
            model_name=serving.model_name,
            **_sizing_log_fields(
                kind=serving.estimate_kind,
                estimate=serving.token_estimator(condensation_input),
                budget_tokens=serving.summary_input_budget,
            ),
        )
        chunk = await _generate_compaction_summary_with_retry(
            sizing=serving,
            acceptance_contexts=acceptance_contexts,
            enforce_acceptance=False,
            retry_policy=retry_policy,
            previous_summary=previous_summary,
            compactable_runs=[],
            initial_summary_input=condensation_input,
            initial_included_runs=[],
            summary_input_budget=serving.summary_input_budget,
            session_id=session_id,
            scope=scope,
            history_settings=history_settings,
            summary_prompt=summary_prompt,
            fallback_sizing=remaining_fallback,
        )
        if chunk.model is not serving.model:
            # A safeguard-refusal switch happened inside the wrapper; the
            # fallback serves the rest of the backstop and keys its verdicts.
            assert remaining_fallback is not None
            serving = remaining_fallback
            remaining_fallback = None
        return chunk.summary

    word_target = min(context.acceptance_limit for context in acceptance_contexts) // _STEERING_ESTIMATOR_UNITS_PER_WORD
    try:
        candidate = await _condense_once(word_target=None, retry_policy=_CONDENSATION_RETRY_POLICY)
        if _block_size(candidate.summary) >= _block_size(previous_summary):
            # The model failed to compress; one strengthened retry with the
            # explicit numeric target, then a terminal marker (I4).
            candidate = await _condense_once(
                word_target=word_target,
                retry_policy=_CONDENSATION_STRENGTHENED_RETRY_POLICY,
            )
    except Exception as exc:
        if not is_context_window_rejection(exc):
            raise
        # Provider-verified impossibility — typed or surfaced through the
        # named legacy message fragments: under the complete-input invariant
        # no replacement can ever be produced by this profile, so record the
        # verdict once and surface the remedies instead of re-buying the same
        # rejection every turn.
        _mark_unfit(stored_digest, "provider_context_window_rejection")
        logger.warning(
            "Compaction condensation rejected by the provider context window",
            session_id=session_id,
            scope=scope.key,
            model_name=serving.model_name,
            summary_input_budget_tokens=serving.summary_input_budget,
            error=str(exc) or type(exc).__name__,
        )
        msg = (
            "the carried summary exceeds the compaction model's context window; "
            "raise the compaction model's context_window, switch compaction.model to a "
            "larger-context model, or force a retry with the compact_context tool"
        )
        raise _CarriedSummaryUnfitError(msg) from exc

    candidate_block_size = _block_size(candidate.summary)
    previous_block_size = _block_size(previous_summary)
    if candidate_block_size >= previous_block_size:
        _mark_unfit(stored_digest, "condensation_not_smaller")
        logger.warning(
            "Compaction condensation did not shrink the carried summary",
            session_id=session_id,
            scope=scope.key,
            model_name=serving.model_name,
            summary_input_estimate=candidate_block_size,
            previous_summary_estimate=previous_block_size,
            summary_input_budget_tokens=serving.summary_input_budget,
        )
        return _CondensationOutcome(persisted_summary=None, serving=serving)

    _persist_summary_only_chunk(
        storage=storage,
        persisted_session=persisted_session,
        working_session=working_session,
        scope=scope,
        summary_text=candidate.summary,
    )
    violation = _acceptance_violation(candidate.summary, acceptance_contexts)
    if violation is None:
        return _CondensationOutcome(persisted_summary=candidate.summary, serving=serving)
    # Strictly smaller but still above an acceptance limit: valid complete-
    # input progress worth keeping — discarding it would re-buy the same call
    # — with a marker on the NEW digest so no automatic attempt follows.
    violating_context, violating_estimate = violation
    _mark_unfit(_summary_digest(candidate.summary), "condensed_summary_still_unfit")
    logger.warning(
        "Compaction condensation shrank the carried summary but it still exceeds the persistable limit",
        session_id=session_id,
        scope=scope.key,
        model_name=violating_context.model_name,
        summary_input_estimate=violating_estimate,
        acceptance_limit=violating_context.acceptance_limit,
        summary_input_budget_tokens=violating_context.summary_input_budget,
    )
    return _CondensationOutcome(persisted_summary=candidate.summary, serving=serving)


def _previous_summary_block(summary: str) -> str:
    return f"<previous_summary>\n{_escape_xml_content(summary)}\n</previous_summary>"


def _messages_for_runs(
    runs: Sequence[RunOutput | TeamRunOutput],
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    messages: list[Message] = []
    for run in runs:
        messages.extend(_compaction_replay_messages(run, history_settings))
    return messages


def _serialize_run(run: RunOutput | TeamRunOutput, index: int, history_settings: ResolvedHistorySettings) -> str:
    lines = [_run_open_tag(run, index)]
    if run.metadata:
        metadata = _metadata_for_summary(run.metadata)
        if metadata:
            lines.extend(["<run_metadata>", _escape_xml_content(stable_serialize(metadata)), "</run_metadata>"])
    for message in _compaction_replay_messages(run, history_settings):
        lines.extend(_serialize_message(message))
    lines.append("</run>")
    return "\n".join(lines)


def _serialize_message(message: Message) -> list[str]:
    lines = [_message_open_tag(message), _escape_xml_content(_render_message_content(message)), "</message>"]
    if message.tool_calls:
        lines.extend(["<tool_calls>", _escape_xml_content(stable_serialize(message.tool_calls)), "</tool_calls>"])
    for tag, media_value in _message_media_entries(message):
        serialized = _serialize_media_payload(media_value)
        if not serialized:
            continue
        lines.extend([f"<{tag}>", _escape_xml_content(serialized), f"</{tag}>"])
    return lines


def _run_open_tag(run: RunOutput | TeamRunOutput, index: int) -> str:
    attrs = [f'index="{index}"']
    if run.run_id:
        attrs.append(f'run_id="{escape(str(run.run_id), quote=True)}"')
    if run.status is not None:
        attrs.append(f'status="{escape(str(run.status), quote=True)}"')
    return f"<run {' '.join(attrs)}>"


def _message_open_tag(message: Message) -> str:
    attrs = [f'role="{escape(message.role, quote=True)}"']
    if message.name:
        attrs.append(f'name="{escape(message.name, quote=True)}"')
    if message.tool_call_id:
        attrs.append(f'tool_call_id="{escape(message.tool_call_id, quote=True)}"')
    return f"<message {' '.join(attrs)}>"


def _message_media_entries(message: Message) -> tuple[tuple[str, object | None], ...]:
    return (
        ("images", message.images),
        ("audio", message.audio),
        ("videos", message.videos),
        ("files", message.files),
        ("audio_output", message.audio_output),
        ("image_output", message.image_output),
        ("video_output", message.video_output),
        ("file_output", message.file_output),
    )


def _serialize_media_payload(media_value: object | None) -> str:
    if media_value is None:
        return ""
    return stable_serialize(_media_payload_snapshot(media_value))


def _media_payload_snapshot(media_value: object) -> object:
    if isinstance(media_value, BaseModel):
        payload = cast("dict[str, object]", media_value.model_dump(exclude_none=True))
        payload.pop("content", None)
        return payload
    if isinstance(media_value, Sequence) and not isinstance(media_value, (str, bytes, bytearray)):
        return [_media_payload_snapshot(item) for item in media_value]
    return media_value


def _render_message_content(message: Message) -> str:
    """Render one replayable string form of a message body."""
    content = message.compressed_content if message.compressed_content is not None else message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(stable_serialize(part) for part in content)
    if content is None:
        return ""
    return stable_serialize(content)


def _unescape_xml_content(text: str) -> str:
    return text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")


def _escape_xml_content(text: str) -> str:
    return escape(_unescape_xml_content(text), quote=False)


def estimate_prompt_visible_history_tokens(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> int:
    """Estimate the durable summary plus visible persisted history for one run."""
    summary_tokens = estimate_session_summary_tokens(_current_summary_text(session))
    history_messages = _history_messages_for_estimation(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    return summary_tokens + _estimate_history_messages_tokens(history_messages)


def estimate_session_summary_tokens(summary_text: str | None) -> int:
    """Estimate prompt-visible tokens contributed by one stored session summary."""
    if summary_text is None:
        return 0
    normalized_summary = summary_text.strip()
    if not normalized_summary:
        return 0
    wrapper = (
        "Here is a brief summary of your previous interactions:\n\n"
        "<summary_of_previous_interactions>\n"
        f"{normalized_summary}\n"
        "</summary_of_previous_interactions>\n\n"
        "Note: this information is from previous interactions and may be outdated. "
        "You should ALWAYS prefer information from this conversation over the past summary.\n\n"
    )
    return estimate_text_tokens(wrapper)


def _estimate_history_messages_tokens(messages: list[Message]) -> int:
    """Estimate the token count of materialized history messages."""
    if not messages:
        return 0
    return sum(_estimated_message_chars(message) for message in messages) // 4


def _strip_stale_anthropic_replay_fields(messages: list[Message]) -> int:
    """Strip stale Anthropic thinking replay fields from completed turns."""
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            last_user_idx = i
            break
    if last_user_idx < 0:
        return 0
    modified = 0
    for msg in messages[:last_user_idx]:
        if msg.role != "assistant":
            continue
        pd = msg.provider_data
        if not isinstance(pd, dict) or "signature" not in pd:
            continue
        msg.reasoning_content = None
        msg.redacted_reasoning_content = None
        del pd["signature"]
        modified += 1
    return modified


def _select_compaction_candidates(
    *,
    visible_runs: list[RunOutput | TeamRunOutput],
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
) -> list[RunOutput | TeamRunOutput]:
    if not visible_runs:
        return []
    if state.force_compact_before_next_run:
        return visible_runs
    if available_history_budget is None:
        return []
    current_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    return visible_runs if current_tokens > available_history_budget else []


def _stable_compaction_run_ids(
    runs: Sequence[RunOutput | TeamRunOutput],
    *,
    session_id: str,
    scope: HistoryScope,
) -> tuple[str, ...]:
    unremovable_run_count = sum(1 for run in runs if not _has_stable_run_id(run))
    if unremovable_run_count:
        logger.warning(
            "Compaction skipped runs without stable run IDs",
            session_id=session_id,
            scope=scope.key,
            skipped_runs=unremovable_run_count,
        )
    return tuple(run.run_id for run in runs if isinstance(run.run_id, str) and run.run_id)


def _history_messages_for_estimation(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    """Return the prompt-visible history messages for token estimation only.

    No deepcopy: filter_tool_calls copies any message it modifies and only the
    list itself is mutated. Stale Anthropic replay fields are left in place
    because the char estimate never counts them.
    """
    history_messages = list(
        _session_history_messages(
            session=session,
            scope=scope,
            history_settings=history_settings,
        ),
    )
    if history_settings.max_tool_calls_from_history is not None and history_messages:
        filter_tool_calls(history_messages, history_settings.max_tool_calls_from_history)
    return history_messages


def _session_history_messages(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    limit = history_settings.policy.limit
    if scope.kind == "team":
        return _team_session_history_messages(
            session=cast("TeamSession", session),
            scope_id=scope.scope_id,
            history_settings=history_settings,
            limit=limit,
        )
    return _agent_session_history_messages(
        session=cast("AgentSession", session),
        scope_id=scope.scope_id,
        history_settings=history_settings,
        limit=limit,
    )


def _agent_session_history_messages(
    *,
    session: AgentSession,
    scope_id: str,
    history_settings: ResolvedHistorySettings,
    limit: int | None,
) -> list[Message]:
    skip_roles = _history_skip_roles(history_settings)
    if history_settings.policy.mode == "runs":
        return session.get_messages(agent_id=scope_id, last_n_runs=limit, skip_roles=skip_roles)
    if history_settings.policy.mode == "messages":
        return session.get_messages(agent_id=scope_id, limit=limit, skip_roles=skip_roles)
    return session.get_messages(agent_id=scope_id, skip_roles=skip_roles)


def _team_session_history_messages(
    *,
    session: TeamSession,
    scope_id: str,
    history_settings: ResolvedHistorySettings,
    limit: int | None,
) -> list[Message]:
    skip_roles = _history_skip_roles(history_settings)
    if history_settings.policy.mode == "runs":
        return session.get_messages(team_id=scope_id, last_n_runs=limit, skip_roles=skip_roles)
    if history_settings.policy.mode == "messages":
        return session.get_messages(team_id=scope_id, limit=limit, skip_roles=skip_roles)
    return session.get_messages(team_id=scope_id, skip_roles=skip_roles)


def _history_skip_roles(history_settings: ResolvedHistorySettings) -> list[str]:
    """Return prompt roles that should never be materialized as persisted history."""
    return sorted(prompt_roles_for_history_storage(history_settings.system_message_role))


def scope_visible_runs(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> list[RunOutput | TeamRunOutput]:
    """Return this scope's model-history-visible runs in stored order."""
    return _runs_for_scope([run for run in session.runs or [] if is_model_history_visible_run(run)], scope)


def _runs_for_scope(
    runs: Sequence[RunOutput | TeamRunOutput],
    scope: HistoryScope,
) -> list[RunOutput | TeamRunOutput]:
    """Filter model-history-visible runs down to one persisted history scope."""
    if scope.kind == "team":
        return [run for run in runs if isinstance(run, TeamRunOutput) and run.team_id == scope.scope_id]
    return [run for run in runs if isinstance(run, RunOutput) and run.agent_id == scope.scope_id]


def _current_summary_text(session: AgentSession | TeamSession) -> str | None:
    if session.summary is None:
        return None
    return session.summary.summary.strip() or None


def _has_stable_run_id(run: RunOutput | TeamRunOutput) -> bool:
    return isinstance(run.run_id, str) and bool(run.run_id)


def _estimated_message_chars(message: Message) -> int:
    content_chars = len(_render_message_content(message))
    tool_call_chars = len(stable_serialize(message.tool_calls)) if message.tool_calls else 0
    return content_chars + tool_call_chars + _estimate_message_media_chars(message)


def _estimate_message_media_chars(message: Message) -> int:
    """Estimate serialized character cost for a message's media payloads."""
    media_chars = 0
    for _tag, media_value in _message_media_entries(message):
        if media_value is None:
            continue
        media_chars += len(stable_serialize(_media_payload_snapshot(media_value)))
    return media_chars


def _model_identifier(model: Model) -> str:
    return model.id or model.__class__.__name__


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
