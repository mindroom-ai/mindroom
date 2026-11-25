"""Pure compaction summarization engine: model loading, cache-warm fork attempt, and XML chunking."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

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
    """Mutable per-compaction chunk state: active serving model, remaining fallback, and shrunk budget."""

    current_model: SummaryModel
    fallback: SummaryModel | None
    shrunk_budget_tokens: int | None = None
    xml_max_attempts: int | None = None


@dataclass(frozen=True)
class SummaryChunk:
    """One produced summary chunk, the stored runs it covers, and the model that served it."""

    summary: str
    included_runs: list[AnyRunRecord]
    model: SummaryModel


@dataclass(frozen=True)
class _SummarySource:
    unread_runs: list[AnyRunRecord]
    previous_summary: str | None
    runs: list[AnyRunRecord]


@timed("system_prompt_assembly.history_prepare.compaction_model_init")
def _load_compaction_model(config: Config, runtime_paths: RuntimePaths, model_name: str) -> ModelProvider:
    return provider_registry.build_model_provider(config, runtime_paths, model_name)


def _summary_model(
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str,
    input_budget_tokens: int,
) -> SummaryModel:
    model = _load_compaction_model(config, runtime_paths, model_name)
    model_config = config.models.get(model_name)
    reasoning = model_config.reasoning if model_config is not None else None
    return SummaryModel(
        model=model,
        name=model_name,
        input_budget_tokens=input_budget_tokens,
        keeps_thinking_setting=reasoning is not None and reasoning.default == "off",
    )


def _compaction_fallback_is_distinct(
    config: Config,
    *,
    primary_model_name: str,
    fallback_model_name: str,
) -> bool:
    """Return whether the configured fallback targets a different ``(provider_route, model_id)``."""
    if fallback_model_name != primary_model_name:
        primary_cfg, fallback_cfg = config.models.get(primary_model_name), config.models.get(fallback_model_name)
        if primary_cfg is None or fallback_cfg is None:
            return True
        if (provider_route(primary_cfg.provider), primary_cfg.id) != (
            provider_route(fallback_cfg.provider),
            fallback_cfg.id,
        ):
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
    assert execution_plan.summary_input_budget_tokens is not None
    primary = _summary_model(
        config,
        runtime_paths,
        execution_plan.compaction_model_name,
        execution_plan.summary_input_budget_tokens,
    )
    fallback_name = execution_plan.compaction_fallback_model_name
    fallback_budget = execution_plan.compaction_fallback_summary_input_budget_tokens
    fallback_model: SummaryModel | None = None
    if (
        fallback_name is not None
        and fallback_budget is not None
        and _compaction_fallback_is_distinct(
            config,
            primary_model_name=execution_plan.compaction_model_name,
            fallback_model_name=fallback_name,
        )
    ):
        try:
            fallback_model = _summary_model(config, runtime_paths, fallback_name, fallback_budget)
        except Exception:
            logger.warning(
                "Compaction fallback model failed to load; continuing without a fallback",
                session_id=session_id,
                scope=scope.key,
                compaction_model=execution_plan.compaction_model_name,
                fallback_model=fallback_name,
                exc_info=True,
            )
    return Summarizer(
        model=primary,
        fallback=fallback_model,
        prompt=config.get_prompt("COMPACTION_SUMMARY_PROMPT"),
        timeout_seconds=execution_plan.compaction_timeout_seconds,
        history_settings=history_settings,
        session_id=session_id,
        scope=scope,
        on_usage=on_usage,
    )


def summary_budget_error(summary_tokens: int, max_summary_tokens: int | None) -> RuntimeError | None:
    """Return an error when a summary exceeds ``max_summary_tokens``, else None."""
    if max_summary_tokens is None or summary_tokens <= max_summary_tokens:
        return None
    return RuntimeError(
        f"The summary exceeds the history budget ({summary_tokens} estimated tokens; {max_summary_tokens} available).",
    )


async def check_summary_budget(
    summary: str,
    max_summary_tokens: int | None,
    model: ModelProvider | None,
) -> RuntimeError | None:
    """Size ``summary`` off the event loop and return a budget error if it exceeds ``max_summary_tokens``."""
    if max_summary_tokens is None or model is None:
        return None
    tokens = await asyncio.to_thread(estimate_summary_text_tokens, summary, model, upper_bound=True)
    return summary_budget_error(tokens, max_summary_tokens)


def _after_native_checkpoint(previous_summary: str | None, runs: Sequence[AnyRunRecord]) -> _SummarySource | None:
    """Split off what the latest readable native checkpoint summarizes, computing ``replay_basis`` lazily."""
    basis_by_index: dict[int, str] = {}
    for index, position, recorded_basis, summary in iter_readable_checkpoints(runs):
        expected = basis_by_index.get(index)
        if expected is None:
            expected = basis_by_index[index] = replay_basis(previous_summary, runs[:index])
        if recorded_basis == expected:
            tail = replace(runs[index], messages=checkpoint_tail(runs[index].messages, position))
            return _SummarySource(list(runs[:index]), summary, [tail, *runs[index + 1 :]])
    return None


def _log_fork_outcome(
    event: str,
    *,
    log_fields: dict[str, object],
    reported_usage: Sequence[MessageMetrics],
    started: float,
    info: bool = False,
    **extra: object,
) -> None:
    tokens = reported_usage[-1].tokens if reported_usage else None
    (logger.info if info else logger.warning)(
        event,
        **log_fields,
        input_tokens=tokens.input_tokens if tokens is not None else None,
        cache_read_tokens=tokens.cache_read_tokens if tokens is not None else None,
        cache_write_tokens=tokens.cache_write_tokens if tokens is not None else None,
        duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
        **extra,
    )


async def _fork_summary_fits(
    summary: str,
    max_summary_tokens: int | None,
    model: ModelProvider | None,
    log_outcome: Callable[..., None],
) -> bool:
    if max_summary_tokens is None or model is None:
        return True
    summary_tokens = await asyncio.to_thread(estimate_summary_text_tokens, summary, model, upper_bound=True)
    if summary_budget_error(summary_tokens, max_summary_tokens) is None:
        return True
    log_outcome(
        "Compaction fork summary exceeded history budget; falling back to XML summary",
        compaction_strategy="xml",
        fork_fallback_reason="exceeds_max_summary_tokens",
        summary_tokens=summary_tokens,
        max_summary_tokens=max_summary_tokens,
    )
    return False


def _fork_fallback_reason(exc: Exception, *, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    return next((label for err, label in _FORK_FALLBACK_REASONS if isinstance(exc, err)), "empty_or_invalid")


async def try_fork_summary(
    summarizer: Summarizer,
    state: ChunkState,
    fork: SummaryFork,
    runs: Sequence[AnyRunRecord],
    *,
    max_summary_tokens: int | None,
    model: ModelProvider | None,
) -> SummaryChunk | None:
    """Attempt one cache-warm fork summary covering all ``runs``, updating ``state`` on fallback."""
    fork_request, skip_reason = await asyncio.to_thread(
        prepare_fork_request,
        fork,
        summary_prompt=summarizer.prompt,
        summary_model_identity=state.current_model.model.identity,
    )
    log_fields: dict[str, object] = {
        "session_id": summarizer.session_id,
        "scope": summarizer.scope.key,
        "model_name": state.current_model.name,
        "mid_turn": fork.mid_turn,
    }
    if fork_request is None:
        logger.info("Compaction fork skipped", **log_fields, compaction_strategy="xml", fork_skip_reason=skip_reason)
        return None

    reported_usage: list[MessageMetrics] = []

    async def on_usage(identity: ModelIdentity, usage: MessageMetrics) -> None:
        reported_usage.append(usage)
        if summarizer.on_usage is not None:
            await summarizer.on_usage(identity, usage)

    started = asyncio.get_running_loop().time()
    log_outcome = partial(_log_fork_outcome, log_fields=log_fields, reported_usage=reported_usage, started=started)
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
    except ModelSafeguardRefusal as exc:
        log_outcome(
            "Compaction fork refused; falling back to XML summary",
            compaction_strategy="xml",
            fork_fallback_reason="safeguard_refusal",
            error=failure_text(exc),
        )
        if state.fallback is not None:
            state.current_model, state.fallback = state.fallback, None
        state.xml_max_attempts = 1
        return None
    except Exception as exc:
        timed_out = is_summary_timeout(exc)
        reason = _fork_fallback_reason(exc, timed_out=timed_out)
        log_outcome(
            "Compaction fork failed; falling back to XML summary",
            compaction_strategy="xml",
            fork_fallback_reason=reason,
            error=failure_text(exc),
        )
        if timed_out:
            state.xml_max_attempts = 1
        return None

    if not await _fork_summary_fits(summary, max_summary_tokens, model, log_outcome):
        return None

    log_outcome("Compaction fork completed", info=True, compaction_strategy="fork")
    return SummaryChunk(summary=summary, included_runs=list(runs), model=state.current_model)


async def summarize_chunk(  # noqa: C901, PLR0915
    summarizer: Summarizer,
    previous_summary: str | None,
    runs: Sequence[AnyRunRecord],
    *,
    state: ChunkState | None = None,
) -> SummaryChunk | None:
    """Summarize the longest XML prefix of ``runs`` that fits one summary input, updating ``state`` in place."""
    chunk_state = state if state is not None else ChunkState(summarizer.model, summarizer.fallback)
    summary_model = chunk_state.current_model
    token_estimator = partial(count, summary_model.model.token_counter, upper_bound=True)
    original = _SummarySource([], previous_summary, list(runs))
    sources = [src for src in (_after_native_checkpoint(previous_summary, runs), original) if src is not None]

    async def build(
        max_input_tokens: int,
        estimator: Callable[[str], int],
    ) -> tuple[str, list[AnyRunRecord], _SummarySource]:
        for candidate in sources:
            built_input, built_runs = await asyncio.to_thread(
                build_summary_input,
                previous_summary=candidate.previous_summary,
                compacted_runs=candidate.runs,
                history_settings=summarizer.history_settings,
                max_input_tokens=max_input_tokens,
                token_estimator=estimator,
            )
            if built_runs:
                return built_input, built_runs, candidate
        return "", [], original

    budget = summary_model.input_budget_tokens
    if chunk_state.shrunk_budget_tokens is not None:
        whole_first_run = await asyncio.to_thread(
            whole_run_input_tokens,
            previous_summary=sources[0].previous_summary,
            first_run=sources[0].runs[0],
            history_settings=summarizer.history_settings,
            token_estimator=token_estimator,
        )
        budget = min(budget, max(chunk_state.shrunk_budget_tokens, whole_first_run))
    summary_input, included_runs, source = await build(budget, token_estimator)
    if not included_runs:
        return None
    retry_policy = (
        DEFAULT_SUMMARY_RETRY_POLICY
        if chunk_state.xml_max_attempts is None
        else replace(DEFAULT_SUMMARY_RETRY_POLICY, max_attempts=chunk_state.xml_max_attempts)
    )
    minimum_progress_input_tokens = await asyncio.to_thread(
        minimum_summary_input_tokens,
        previous_summary=source.previous_summary,
        first_run=source.runs[0],
        token_estimator=token_estimator,
    )
    attempt = 1
    while True:
        summary_input_estimate = await asyncio.to_thread(token_estimator, summary_input)
        attempt_fields = {
            "session_id": summarizer.session_id,
            "scope": summarizer.scope.key,
            "model_name": summary_model.name,
            "attempt": attempt,
            "native_checkpoint": source is not original,
            "native_checkpoint_runs": len(source.unread_runs),
            "candidate_runs": len(source.runs),
            "included_runs": len(included_runs),
            "summary_input_estimate": summary_input_estimate,
            "summary_input_budget_tokens": budget,
            "timeout_seconds": summarizer.timeout_seconds,
        }
        started = asyncio.get_running_loop().time()
        logger.info("Compaction summary chunk request", **attempt_fields)
        try:
            summary = await generate_compaction_summary(
                model=summary_model.model,
                summary_input=summary_input,
                summary_prompt=summarizer.prompt,
                timeout_seconds=summarizer.timeout_seconds,
                keep_thinking_setting=summary_model.keeps_thinking_setting,
                on_usage=summarizer.on_usage,
                allow_transient_fallback=attempt >= retry_policy.max_attempts,
            )
        except Exception as exc:
            logger.warning(
                "Compaction summary chunk failed",
                **attempt_fields,
                duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                error=failure_text(exc),
            )
            fallback_model = chunk_state.fallback
            if (
                fallback_model is not None
                and attempt < retry_policy.max_attempts
                and isinstance(exc, ModelSafeguardRefusal)
                and not (
                    exc.model_id is not None
                    and (provider_route(exc.provider), exc.model_id)
                    == (provider_route(fallback_model.model.identity.provider), fallback_model.model.identity.id)
                )
            ):
                fallback_estimator = partial(count, fallback_model.model.token_counter, upper_bound=True)
                fits_fallback = (
                    await asyncio.to_thread(fallback_estimator, summary_input) <= fallback_model.input_budget_tokens
                )
                rebuilt_input, rebuilt_runs, rebuilt_source = (
                    (summary_input, included_runs, source)
                    if fits_fallback
                    else await build(fallback_model.input_budget_tokens, fallback_estimator)
                )
                if not rebuilt_runs:
                    raise
                logger.info(
                    "Compaction summary refused; switching to fallback model",
                    session_id=summarizer.session_id,
                    scope=summarizer.scope.key,
                    attempt=attempt,
                    refused_model=summary_model.name,
                    fallback_model=fallback_model.name,
                    fallback_summary_input_budget_tokens=fallback_model.input_budget_tokens,
                )
                summary_model = chunk_state.current_model = fallback_model
                chunk_state.fallback = chunk_state.shrunk_budget_tokens = None
                summary_input, included_runs, source = rebuilt_input, rebuilt_runs, rebuilt_source
                budget, token_estimator = fallback_model.input_budget_tokens, fallback_estimator
                attempt += 1
                continue
            retry_decision = retry_policy.retry_budget(
                attempt=attempt,
                budget=budget,
                input_tokens=summary_input_estimate,
                minimum_progress_input_tokens=minimum_progress_input_tokens,
                error=exc,
            )
            if retry_decision is None:
                raise
            if retry_decision.kind == "same-budget-transient":
                await asyncio.sleep(retry_policy.same_input_retry_delay_seconds)
                attempt += 1
                continue
            rebuilt_input, rebuilt_runs, rebuilt_source = await build(retry_decision.budget, token_estimator)
            if not rebuilt_runs or await asyncio.to_thread(token_estimator, rebuilt_input) >= summary_input_estimate:
                raise
            summary_input, included_runs, source = rebuilt_input, rebuilt_runs, rebuilt_source
            budget = chunk_state.shrunk_budget_tokens = retry_decision.budget
            attempt += 1
            continue
        logger.info(
            "Compaction summary chunk completed",
            **attempt_fields,
            compaction_strategy="xml",
            duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
        )
        chunk_state.xml_max_attempts = None
        folded = list(runs[: len(source.unread_runs) + len(included_runs)])
        return SummaryChunk(summary=summary, included_runs=folded, model=summary_model)


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
    """Summarize ``runs`` into one cumulative summary without writing anything."""
    state = ChunkState(summarizer.model, summarizer.fallback)
    if fork is not None and runs:
        fork_chunk = await try_fork_summary(
            summarizer,
            state,
            fork,
            runs,
            max_summary_tokens=max_summary_tokens,
            model=model,
        )
        if fork_chunk is not None:
            return fork_chunk.summary, fork_chunk.model
    summary = previous_summary
    remaining = list(runs)
    while remaining:
        chunk = await summarize_chunk(summarizer, summary, remaining, state=state)
        if chunk is None:
            msg = "No compactable content fit the summary input budget."
            raise RuntimeError(msg)
        summary = chunk.summary
        remaining = remaining[len(chunk.included_runs) :]
    assert summary is not None
    if enforce_xml_budget and (error := await check_summary_budget(summary, max_summary_tokens, model)) is not None:
        raise error
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
    "summary_budget_error",
    "try_fork_summary",
]
