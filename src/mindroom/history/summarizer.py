"""Pure compaction summarization engine: model loading, cache-warm fork attempt, and XML chunking."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

from mindroom.failure_text import failure_text
from mindroom.history.replay import estimate_summary_text_tokens
from mindroom.history.summary_call import (
    DEFAULT_SUMMARY_RETRY_POLICY,
    CompactionForkToolCallError,
    CompactionSummaryIncompleteError,
    CompactionSummaryOutputLimitError,
    generate_compaction_summary,
    is_summary_timeout,
    request_compaction_summary,
)
from mindroom.history.summary_fork import prepare_fork_request
from mindroom.history.summary_input import build_summary_input, minimum_summary_input_tokens, whole_run_input_tokens
from mindroom.llm.errors import AuthenticationFailed, ContextWindowExceeded, ModelSafeguardRefusal, ProviderError
from mindroom.logging_config import get_logger
from mindroom.native_compaction import checkpoint_tail, iter_readable_checkpoints, replay_basis
from mindroom.providers import registry as provider_registry
from mindroom.providers.names import provider_route
from mindroom.timing import timed
from mindroom.token_counting.counts import count

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.summary_fork import SummaryFork
    from mindroom.history.types import HistoryScope, ResolvedHistoryExecutionPlan, ResolvedHistorySettings
    from mindroom.llm.provider import ModelIdentity, ModelProvider
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import MessageMetrics

logger = get_logger(__name__)

_FORK_FALLBACK_REASONS: tuple[tuple[type[Exception], str], ...] = (
    (CompactionForkToolCallError, "tool_call"),
    (CompactionSummaryOutputLimitError, "output_limit"),
    (CompactionSummaryIncompleteError, "incomplete"),
    (ContextWindowExceeded, "context_window_exceeded"),
    (ProviderError, "provider_error"),
)


@dataclass(frozen=True)
class SummaryModel:
    """One serving model with its configured alias and serialized-input budget."""

    model: ModelProvider
    name: str
    input_budget_tokens: int
    keeps_thinking_setting: bool = False


@dataclass(frozen=True)
class Summarizer:
    """Immutable summary model and request settings shared across every chunk of one compaction."""

    model: SummaryModel
    fallback: SummaryModel | None
    prompt: str
    timeout_seconds: float
    history_settings: ResolvedHistorySettings
    session_id: str
    scope: HistoryScope
    on_usage: Callable[[ModelIdentity, MessageMetrics], Awaitable[None]] | None


@dataclass
class ChunkState:
    """Mutable per-compaction chunk state: active model, fallback, one-shot fork candidate, and shrunk budget."""

    current_model: SummaryModel
    fallback: SummaryModel | None
    fork: SummaryFork | None = None
    shrunk_budget_tokens: int | None = None
    xml_max_attempts: int | None = None


@dataclass(frozen=True)
class SummaryChunk:
    """One produced summary chunk, the stored runs it covers, the serving model, and whether a warm fork produced it."""

    summary: str
    included_runs: list[AnyRunRecord]
    model: SummaryModel
    from_fork: bool = False


class _SummarySource(NamedTuple):
    unread_runs: list[AnyRunRecord]
    previous_summary: str | None
    runs: list[AnyRunRecord]


@timed("system_prompt_assembly.history_prepare.compaction_model_init")
def _load_compaction_model(config: Config, runtime_paths: RuntimePaths, model_name: str) -> ModelProvider:
    return provider_registry.build_model_provider(config, runtime_paths, model_name)


def _summary_model(config: Config, paths: RuntimePaths, name: str, budget: int) -> SummaryModel:
    cfg = config.models.get(name)
    keeps = bool(cfg and cfg.reasoning and cfg.reasoning.default == "off")
    return SummaryModel(_load_compaction_model(config, paths, name), name, budget, keeps)


def _compaction_fallback_is_distinct(config: Config, *, primary_model_name: str, fallback_model_name: str) -> bool:
    if fallback_model_name != primary_model_name:
        a, b = config.models.get(primary_model_name), config.models.get(fallback_model_name)
        if a is None or b is None or (provider_route(a.provider), a.id) != (provider_route(b.provider), b.id):
            return True
    logger.warning(
        "Compaction fallback resolves to the primary serving model; continuing without a fallback",
        compaction_model=primary_model_name,
        fallback_model=fallback_model_name,
    )
    return False


def load_summarizer(
    execution_plan: ResolvedHistoryExecutionPlan,
    history_settings: ResolvedHistorySettings,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    session_id: str,
    scope: HistoryScope,
    on_usage: Callable[[ModelIdentity, MessageMetrics], Awaitable[None]] | None,
) -> Summarizer:
    """Load the summary model and, when distinct and loadable, its safeguard fallback."""
    plan = execution_plan
    assert plan.summary_input_budget_tokens is not None
    primary = _summary_model(config, runtime_paths, plan.compaction_model_name, plan.summary_input_budget_tokens)
    fb_name, fb_budget = plan.compaction_fallback_model_name, plan.compaction_fallback_summary_input_budget_tokens
    fallback: SummaryModel | None = None
    if (
        fb_name
        and fb_budget is not None
        and _compaction_fallback_is_distinct(
            config,
            primary_model_name=plan.compaction_model_name,
            fallback_model_name=fb_name,
        )
    ):
        try:
            fallback = _summary_model(config, runtime_paths, fb_name, fb_budget)
        except Exception:
            logger.warning(
                "Compaction fallback model failed to load; continuing without a fallback",
                session_id=session_id,
                scope=scope.key,
                compaction_model=plan.compaction_model_name,
                fallback_model=fb_name,
                exc_info=True,
            )
    prompt, timeout = config.get_prompt("COMPACTION_SUMMARY_PROMPT"), plan.compaction_timeout_seconds
    return Summarizer(primary, fallback, prompt, timeout, history_settings, session_id, scope, on_usage)


async def check_summary_budget(
    chunk: SummaryChunk,
    max_summary_tokens: int | None,
    model: ModelProvider | None,
) -> RuntimeError | None:
    """Return a budget error when a non-fork chunk's summary exceeds ``max_summary_tokens``."""
    if chunk.from_fork or max_summary_tokens is None or model is None:
        return None
    tokens = await asyncio.to_thread(estimate_summary_text_tokens, chunk.summary, model, upper_bound=True)
    if tokens <= max_summary_tokens:
        return None
    msg = f"The summary exceeds the history budget ({tokens} estimated tokens; {max_summary_tokens} available)."
    return RuntimeError(msg)


def _after_native_checkpoint(previous_summary: str | None, runs: Sequence[AnyRunRecord]) -> _SummarySource | None:
    """Split off what the latest readable native checkpoint summarizes, computing ``replay_basis`` lazily.

    A provider checkpoint stands in only while its recorded basis still matches ``previous_summary`` and the prior
    runs (an approval-paused run sorts after the turns it waited for, and retiring a summary keeps its runs). Its own
    run stays from the checkpoint on so the resulting summary always folds that run, and editing or redacting it
    removes the summary too.
    """
    basis_by_index: dict[int, str] = {}
    for index, position, recorded_basis, summary in iter_readable_checkpoints(runs):
        if (expected := basis_by_index.get(index)) is None:
            expected = basis_by_index[index] = replay_basis(previous_summary, runs[:index])
        if recorded_basis == expected:
            tail = replace(runs[index], messages=checkpoint_tail(runs[index].messages, position))
            return _SummarySource(list(runs[:index]), summary, [tail, *runs[index + 1 :]])
    return None


def _elapsed_ms(started: float) -> int:
    return int((asyncio.get_running_loop().time() - started) * 1000)


def _handle_fork_failure(exc: Exception, state: ChunkState, log_outcome: Callable[..., None]) -> None:
    if isinstance(exc, ModelSafeguardRefusal):
        event, reason, cap_xml = "Compaction fork refused; falling back to XML summary", "safeguard_refusal", True
        if state.fallback is not None:
            state.current_model, state.fallback = state.fallback, None
    else:
        cap_xml = is_summary_timeout(exc)
        fallback = next((r for e, r in _FORK_FALLBACK_REASONS if isinstance(exc, e)), "empty_or_invalid")
        event, reason = "Compaction fork failed; falling back to XML summary", "timeout" if cap_xml else fallback
    log_outcome(event, compaction_strategy="xml", fork_fallback_reason=reason, error=failure_text(exc))
    if cap_xml:
        state.xml_max_attempts = 1


async def _try_fork_summary(
    summarizer: Summarizer,
    state: ChunkState,
    fork: SummaryFork,
    runs: Sequence[AnyRunRecord],
    *,
    max_summary_tokens: int | None,
    model: ModelProvider | None,
) -> SummaryChunk | None:
    fork_request, skip_reason = await asyncio.to_thread(
        prepare_fork_request,
        fork,
        summary_prompt=summarizer.prompt,
        summary_model_identity=state.current_model.model.identity,
    )
    fields: dict[str, object] = {
        "session_id": summarizer.session_id,
        "scope": summarizer.scope.key,
        "model_name": state.current_model.name,
        "mid_turn": fork.mid_turn,
    }
    if fork_request is None:
        logger.info("Compaction fork skipped", **fields, compaction_strategy="xml", fork_skip_reason=skip_reason)
        return None

    reported_usage: list[MessageMetrics] = []
    started = asyncio.get_running_loop().time()

    async def on_usage(identity: ModelIdentity, usage: MessageMetrics) -> None:
        reported_usage.append(usage)
        if summarizer.on_usage is not None:
            await summarizer.on_usage(identity, usage)

    def log_outcome(event: str, *, info: bool = False, **extra: object) -> None:
        tok = reported_usage[-1].tokens if reported_usage else None
        toks = {k: getattr(tok, k, None) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")}
        log = logger.info if info else logger.warning
        log(event, **fields, **toks, duration_ms=_elapsed_ms(started), **extra)

    try:
        summary = await request_compaction_summary(
            model=fork.sizer.model,
            request=fork_request,
            timeout_seconds=summarizer.timeout_seconds,
            on_usage=on_usage,
            reject_tool_calls=True,
        )
    except (AuthenticationFailed, asyncio.CancelledError):
        raise
    except Exception as exc:
        _handle_fork_failure(exc, state, log_outcome)
        return None

    if max_summary_tokens is not None and model is not None:
        summary_tokens = await asyncio.to_thread(estimate_summary_text_tokens, summary, model, upper_bound=True)
        if summary_tokens > max_summary_tokens:
            log_outcome(
                "Compaction fork summary exceeded history budget; falling back to XML summary",
                compaction_strategy="xml",
                fork_fallback_reason="exceeds_max_summary_tokens",
                summary_tokens=summary_tokens,
                max_summary_tokens=max_summary_tokens,
            )
            return None

    log_outcome("Compaction fork completed", info=True, compaction_strategy="fork")
    return SummaryChunk(summary, list(runs), state.current_model, from_fork=True)


def _safeguard_fallback(exc: Exception, fb: SummaryModel | None, attempt: int, limit: int) -> SummaryModel | None:
    if fb is None or attempt >= limit or not isinstance(exc, ModelSafeguardRefusal):
        return None
    fb_id = (provider_route(fb.model.identity.provider), fb.model.identity.id)
    return None if exc.model_id is not None and (provider_route(exc.provider), exc.model_id) == fb_id else fb


async def _build_from_sources(
    sources: Sequence[_SummarySource],
    settings: ResolvedHistorySettings,
    max_tokens: int,
    est: Callable[[str], int],
) -> tuple[str, list[AnyRunRecord], _SummarySource]:
    for src in sources:
        text, built = await asyncio.to_thread(
            build_summary_input,
            previous_summary=src.previous_summary,
            compacted_runs=src.runs,
            history_settings=settings,
            max_input_tokens=max_tokens,
            token_estimator=est,
        )
        if built:
            return text, built, src
    return "", [], sources[-1]


async def summarize_chunk(
    summarizer: Summarizer,
    previous_summary: str | None,
    runs: Sequence[AnyRunRecord],
    *,
    state: ChunkState | None = None,
    max_summary_tokens: int | None = None,
    model: ModelProvider | None = None,
) -> SummaryChunk | None:
    """Produce one summary chunk: tries ``state.fork`` once on the first pass, else summarizes an XML prefix."""
    st = state if state is not None else ChunkState(summarizer.model, summarizer.fallback)
    if st.fork is not None and runs:
        fork, st.fork = st.fork, None
        chunk = await _try_fork_summary(summarizer, st, fork, runs, max_summary_tokens=max_summary_tokens, model=model)
        if chunk is not None:
            return chunk
    return await _summarize_xml_chunk(summarizer, previous_summary, runs, st)


async def _summarize_xml_chunk(  # noqa: PLR0915
    summarizer: Summarizer,
    previous_summary: str | None,
    runs: Sequence[AnyRunRecord],
    st: ChunkState,
) -> SummaryChunk | None:
    summary_model = st.current_model
    estimator = partial(count, summary_model.model.token_counter, upper_bound=True)
    original = _SummarySource([], previous_summary, list(runs))
    sources = [src for src in (_after_native_checkpoint(previous_summary, runs), original) if src is not None]
    settings = summarizer.history_settings
    build = partial(_build_from_sources, sources, settings)

    budget = summary_model.input_budget_tokens
    if st.shrunk_budget_tokens is not None:
        first_tokens = await asyncio.to_thread(
            whole_run_input_tokens,
            previous_summary=sources[0].previous_summary,
            first_run=sources[0].runs[0],
            history_settings=settings,
            token_estimator=estimator,
        )
        budget = min(budget, max(st.shrunk_budget_tokens, first_tokens))
    summary_input, included_runs, source = await build(budget, estimator)
    if not included_runs:
        return None
    retry = replace(
        DEFAULT_SUMMARY_RETRY_POLICY,
        max_attempts=st.xml_max_attempts or DEFAULT_SUMMARY_RETRY_POLICY.max_attempts,
    )
    min_tokens = await asyncio.to_thread(
        minimum_summary_input_tokens,
        previous_summary=source.previous_summary,
        first_run=source.runs[0],
        token_estimator=estimator,
    )
    attempt = 1
    while True:
        input_est = await asyncio.to_thread(estimator, summary_input)
        fields = {
            "session_id": summarizer.session_id,
            "scope": summarizer.scope.key,
            "model_name": summary_model.name,
            "attempt": attempt,
            "native_checkpoint": source is not original,
            "native_checkpoint_runs": len(source.unread_runs),
            "candidate_runs": len(source.runs),
            "included_runs": len(included_runs),
            "summary_input_estimate": input_est,
            "summary_input_budget_tokens": budget,
            "timeout_seconds": summarizer.timeout_seconds,
        }
        started = asyncio.get_running_loop().time()
        logger.info("Compaction summary chunk request", **fields)
        try:
            summary = await generate_compaction_summary(
                model=summary_model.model,
                summary_input=summary_input,
                summary_prompt=summarizer.prompt,
                timeout_seconds=summarizer.timeout_seconds,
                keep_thinking_setting=summary_model.keeps_thinking_setting,
                on_usage=summarizer.on_usage,
                allow_transient_fallback=attempt >= retry.max_attempts,
            )
        except Exception as exc:
            ms = _elapsed_ms(started)
            logger.warning("Compaction summary chunk failed", **fields, duration_ms=ms, error=failure_text(exc))
            if (fb := _safeguard_fallback(exc, st.fallback, attempt, retry.max_attempts)) is not None:
                fb_est = partial(count, fb.model.token_counter, upper_bound=True)
                fits = await asyncio.to_thread(fb_est, summary_input) <= fb.input_budget_tokens
                rebuilt = (
                    (summary_input, included_runs, source) if fits else await build(fb.input_budget_tokens, fb_est)
                )
                if not rebuilt[1]:
                    raise
                logger.info(
                    "Compaction summary refused; switching to fallback model",
                    session_id=summarizer.session_id,
                    scope=summarizer.scope.key,
                    attempt=attempt,
                    refused_model=summary_model.name,
                    fallback_model=fb.name,
                    fallback_summary_input_budget_tokens=fb.input_budget_tokens,
                )
                summary_model = st.current_model = fb
                st.fallback = st.shrunk_budget_tokens = None
                summary_input, included_runs, source = rebuilt
                budget, estimator = fb.input_budget_tokens, fb_est
            else:
                decision = retry.retry_budget(
                    attempt=attempt,
                    budget=budget,
                    input_tokens=input_est,
                    minimum_progress_input_tokens=min_tokens,
                    error=exc,
                )
                if decision is None:
                    raise
                if decision.kind == "same-budget-transient":
                    await asyncio.sleep(retry.same_input_retry_delay_seconds)
                else:
                    rebuilt = await build(decision.budget, estimator)
                    if not rebuilt[1] or await asyncio.to_thread(estimator, rebuilt[0]) >= input_est:
                        raise
                    summary_input, included_runs, source = rebuilt
                    budget = st.shrunk_budget_tokens = decision.budget
            attempt += 1
            continue
        ms = _elapsed_ms(started)
        logger.info("Compaction summary chunk completed", **fields, compaction_strategy="xml", duration_ms=ms)
        st.xml_max_attempts = None
        folded = list(runs[: len(source.unread_runs) + len(included_runs)])
        return SummaryChunk(summary, folded, summary_model)


async def summarize_runs(
    summarizer: Summarizer,
    previous_summary: str | None,
    runs: Sequence[AnyRunRecord],
    *,
    fork: SummaryFork | None = None,
    max_summary_tokens: int | None = None,
    model: ModelProvider | None = None,
    enforce_xml_budget: bool = False,
) -> tuple[str, SummaryModel]:
    """Summarize ``runs`` into one cumulative summary without writing anything.

    When ``enforce_xml_budget`` is False (run-local mid-turn compaction), ``max_summary_tokens`` still gates the warm
    fork so an oversized fork falls back to XML while the final run-local request is checked against ``fit_limit``.
    """
    state = ChunkState(summarizer.model, summarizer.fallback, fork=fork)
    summary, remaining, last_chunk = previous_summary, list(runs), None
    while remaining:
        last_chunk = await summarize_chunk(
            summarizer,
            summary,
            remaining,
            state=state,
            max_summary_tokens=max_summary_tokens,
            model=model,
        )
        if last_chunk is None:
            msg = "No compactable content fit the summary input budget."
            raise RuntimeError(msg)
        summary, remaining = last_chunk.summary, remaining[len(last_chunk.included_runs) :]
    assert summary is not None
    err = (
        await check_summary_budget(last_chunk, max_summary_tokens, model) if enforce_xml_budget and last_chunk else None
    )
    if err is not None:
        raise err
    return summary, state.current_model


__all__ = [
    "ChunkState",
    "Summarizer",
    "SummaryChunk",
    "SummaryModel",
    "check_summary_budget",
    "load_summarizer",
    "summarize_chunk",
    "summarize_runs",
]
