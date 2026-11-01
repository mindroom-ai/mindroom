"""Build Matrix-visible AI run metadata from model usage counters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.budgets.pricing import format_cost_usd, run_cost_usd
from mindroom.constants import AI_RUN_METADATA_KEY, PRIOR_USAGE_RUN_METADATA_KEY
from mindroom.llm.messages import is_own_model_message
from mindroom.llm.usage import RunMetrics
from mindroom.model_usage import ContextTokens, message_context_tokens, uncached_input_tokens_from_counts

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.budgets.pricing import PricedModel
    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.history.types import PreparedHistoryState
    from mindroom.llm.runs import AnyRunRecord, RunStatus
    from mindroom.llm.usage import TokenCounts

_AI_RUN_METADATA_VERSION = 1
# The ``usage`` object lists the counters first, in this order; a counter it omits follows them. It equals the stored
# order (``llm/usage.py``) today but is its own tuple: the published event and the stored row are separate contracts.
_PUBLISHED_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "audio_input_tokens",
    "audio_output_tokens",
    "audio_total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


@dataclass(frozen=True, slots=True)
class ReplyUsage:
    """The ``usage`` object of a reply's ``io.mindroom.ai_run`` metadata, kept apart from how runs store usage.

    A delegated child's record reports its usage in this shape too. Zero counters and unset values are left out, and
    fractions are published as decimal strings, because Matrix event JSON forbids floats.
    """

    tokens: TokenCounts
    time_to_first_token: float | None
    duration: float | None
    # None when the provider reported no usage, whose input then has no split to publish.
    uncached_input_tokens: int | None

    @classmethod
    def of(
        cls,
        metrics: RunMetrics,
        *,
        provider: str | None,
        configured_provider: str | None,
        model_id: str | None,
    ) -> ReplyUsage:
        """Return the usage a run's metrics publish, for the model that answered when ``metrics`` has no breakdown."""
        uncached_input_tokens = (
            _uncached_input_tokens(
                metrics,
                provider=provider,
                configured_provider=configured_provider,
                model_id=model_id,
            )
            if metrics.reported
            else None
        )
        return cls(metrics.tokens, metrics.time_to_first_token, metrics.duration, uncached_input_tokens)

    def to_wire(self) -> dict[str, Any]:
        """Return the published object; the uncached input is published as given, zero included."""
        values = {
            **self.tokens.to_dict(first=_PUBLISHED_COUNTERS),
            "time_to_first_token": self.time_to_first_token,
            "duration": self.duration,
        }
        wire: dict[str, Any] = {
            key: format(value, ".12g") if isinstance(value, float) else value
            for key, value in values.items()
            if isinstance(value, (float, str)) or (isinstance(value, int) and value != 0)
        }
        if self.uncached_input_tokens is not None:
            wire["uncached_input_tokens"] = self.uncached_input_tokens
        return wire


def _build_context_payload(context: ContextTokens | None, model_config: ModelConfig | None) -> dict[str, Any] | None:
    if context is None or model_config is None or model_config.context_window is None:
        return None
    payload = {
        "input_tokens": context.tokens,
        "window_tokens": model_config.context_window,
    }
    if context.cache_read_input_tokens > 0:
        payload["cache_read_input_tokens"] = context.cache_read_input_tokens
    # The split into cache reads and the rest is shown only when the provider reported a cache counter.
    if context.cache_read_tokens is None and context.cache_write_tokens is None:
        return payload
    payload["uncached_input_tokens"] = context.uncached_input_tokens
    cache_write_tokens = context.cache_write_tokens
    if cache_write_tokens is not None and 0 < cache_write_tokens <= context.uncached_input_tokens:
        payload["cache_write_input_tokens"] = cache_write_tokens
    return payload


def _prepared_history_payload(prepared_history: PreparedHistoryState) -> dict[str, Any]:
    """Return the prepared-context and compaction diagnostics a reply's run metadata publishes."""
    payload: dict[str, Any] = {}
    if prepared_history.prepared_context_tokens is not None:
        payload["prepared_context"] = {"tokens": prepared_history.prepared_context_tokens}
    decision = prepared_history.compaction_decision
    compaction: dict[str, Any] = {
        "decision": decision.mode,
        "outcome": prepared_history.compaction_reply_outcome,
        "reason": decision.reason,
    }
    if decision.current_history_tokens is not None:
        compaction["current_history_tokens"] = decision.current_history_tokens
    if decision.trigger_budget_tokens is not None:
        compaction["trigger_budget_tokens"] = decision.trigger_budget_tokens
    if decision.hard_budget_tokens is not None:
        compaction["hard_budget_tokens"] = decision.hard_budget_tokens
    if decision.fitted_replay_tokens is not None:
        compaction["fitted_replay_tokens"] = decision.fitted_replay_tokens
    if prepared_history.replay_plan is not None:
        compaction["replay_plan"] = {
            "mode": prepared_history.replay_plan.mode,
            "estimated_tokens": prepared_history.replay_plan.estimated_tokens,
        }
    payload["compaction"] = compaction
    return payload


def build_prepared_history_metadata_content(prepared_history: PreparedHistoryState) -> dict[str, Any]:
    """Build Matrix message metadata for prepared-context and compaction diagnostics."""
    return {AI_RUN_METADATA_KEY: {"version": _AI_RUN_METADATA_VERSION, **_prepared_history_payload(prepared_history)}}


def ai_run_extra_content_from_metadata(run_metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the Matrix-visible AI run metadata subset from persisted run metadata."""
    if run_metadata is None:
        return None
    ai_run_metadata = run_metadata.get(AI_RUN_METADATA_KEY)
    if not isinstance(ai_run_metadata, dict):
        return None
    return {AI_RUN_METADATA_KEY: dict(ai_run_metadata)}


def reply_cost_usd(content: Mapping[str, Any] | None, *, completed: bool) -> float | None:
    """Return the USD cost a reply's AI run metadata reports, or None when the cost is unknown.

    A completed reply publishes its run metadata, which leaves the cost out only when no priced model was used, so its
    cost is zero then. Another ending, such as a stopped or failed run, can leave usage that no metadata reports.
    """
    ai_run_metadata = (content or {}).get(AI_RUN_METADATA_KEY)
    cost = ai_run_metadata.get("cost") if isinstance(ai_run_metadata, dict) else None
    usd = cost.get("usd") if isinstance(cost, dict) else None
    if isinstance(usd, str):
        return float(usd)
    return 0.0 if completed else None


def _reply_context_tokens(
    run: AnyRunRecord | None,
    prepared_history: PreparedHistoryState | None,
    *,
    provider: str | None,
    configured_provider: str | None,
    model_id: str | None,
) -> ContextTokens | None:
    """Return the context the run's last own model request reported, else the pre-flight estimate of its history.

    The estimate stands in when the run holds no request its model answered, and when the last one reported no usage
    of its own: on a route that reports none, or for a retried request whose usage sums its attempts.
    """
    newest_first = reversed(run.messages) if run is not None else ()
    last = next((message for message in newest_first if is_own_model_message(message)), None)
    if last is not None:
        reported = message_context_tokens(
            last,
            provider=provider,
            configured_provider=configured_provider,
            model_id=model_id,
        )
        if reported is not None:
            return reported
    estimate = prepared_history.prepared_context_tokens if prepared_history is not None else None
    return None if estimate is None else ContextTokens(estimate)


def _uncached_input_tokens(
    metrics: RunMetrics,
    *,
    provider: str | None,
    configured_provider: str | None,
    model_id: str | None,
) -> int | None:
    """Split new input from cache reads per model, since providers count cache tokens differently."""
    entries = [entry for entries in (metrics.details or {}).values() for entry in entries]
    if entries:
        return sum(
            uncached_input_tokens_from_counts(
                input_tokens=entry.tokens.input_tokens,
                cache_read_tokens=entry.tokens.cache_read_tokens,
                cache_write_tokens=entry.tokens.cache_write_tokens,
                provider=entry.provider,
                configured_provider=None,
                model_id=entry.id,
            )
            or 0
            for entry in entries
        )
    # A zero counter counts as unreported, as the published usage leaves it out.
    return uncached_input_tokens_from_counts(
        input_tokens=metrics.tokens.input_tokens or None,
        cache_read_tokens=metrics.tokens.cache_read_tokens or None,
        cache_write_tokens=metrics.tokens.cache_write_tokens or None,
        provider=provider,
        configured_provider=configured_provider,
        model_id=model_id,
    )


def turn_usage(prior_usage: Sequence[RunMetrics], metrics: RunMetrics | None) -> RunMetrics | None:
    """Return the usage of a turn: its spent attempts' ``prior_usage``, then ``metrics``, the final attempt's.

    A reply and a delegated child's record both report their turn's usage so. A turn of one attempt reports that
    attempt's usage as it is. Otherwise counters, durations, and per-model details add up, and the turn's first
    token is its first attempt's that reported one, as a run's is its first request's.
    """
    if not prior_usage:
        return metrics
    attempts = [*prior_usage, *(() if metrics is None else (metrics,))]
    total = sum(attempts, RunMetrics())
    total.time_to_first_token = next(
        (usage.time_to_first_token for usage in attempts if usage.time_to_first_token is not None),
        None,
    )
    return total


def with_prior_usage(metadata: dict[str, Any] | None, prior_usage: Sequence[RunMetrics]) -> dict[str, Any] | None:
    """Return the metadata a run's record stores: ``metadata``, with ``prior_usage``, what its turn spent before it.

    The record keeps that usage summed (``turn_usage``), so whichever resume continues the run after a pause, even
    after a restart, reports it with the run's own (``prior_usage_of``). A turn's first run keeps none. ``metadata``
    is the turn's, which holds no other run's usage (``without_run_usage``).
    """
    usage = turn_usage(prior_usage, None)
    return metadata if usage is None else {**(metadata or {}), PRIOR_USAGE_RUN_METADATA_KEY: usage.to_dict()}


def prior_usage_of(record: AnyRunRecord) -> tuple[RunMetrics, ...]:
    """Return the usage the turn of ``record``'s run spent on attempts before it, as ``with_prior_usage`` stored it."""
    # LEGACY_COMPAT: a run stored without the usage its turn spent before it.
    # Legacy format: a run record whose metadata has no ``mindroom_prior_usage`` key though its turn spent attempts
    # before it, such as an empty answer the turn retried or a run a model switch continued; it matters for a run that
    # is still paused.
    # Last legacy release: v2026.10.733 stored every run without the key; the next release stores it on each run whose
    # turn spent earlier attempts.
    # Handling: such a run reads as its turn's first, so its resume reports the paused run and what follows, as those
    # releases did; the usage ledger counts the earlier attempts either way.
    # Coverage: tests/test_approval_turn_continuation.py::test_a_pause_stored_without_its_turn_s_earlier_usage_resumes_publishing_the_paused_run_s.
    stored = (record.metadata or {}).get(PRIOR_USAGE_RUN_METADATA_KEY)
    return (RunMetrics.from_dict(stored),) if isinstance(stored, dict) else ()


def build_ai_run_metadata_content(
    *,
    config: Config,
    model_name: str,
    run_id: str,
    session_id: str,
    status: RunStatus,
    model: str | None,
    model_provider: str | None,
    prices: Mapping[tuple[str, str], PricedModel],
    tool_count: int,
    metrics: RunMetrics | None = None,
    prior_usage: Sequence[RunMetrics] = (),
    context_run: AnyRunRecord | None = None,
    prepared_history: PreparedHistoryState | None = None,
) -> dict[str, Any]:
    """Build the Matrix event content fragment for a reply whose turn ended with one AI run.

    `model_name` is the configured model name resolved at run preparation time.
    It must not be re-resolved here: the per-thread override store can change
    mid-run (for example via `switch_thread_model`), and this metadata must
    describe the model that actually produced the response.
    `metrics` is the final run's usage and `prior_usage` that of the turn's spent attempts, such as an empty reply that
    was retried or a run a model switch continued; the reply's usage and cost cover them all. Everything else
    describes the final run: its model, run id, status, tool count, and context.
    `context_run` is the run whose last own model request's context the reply reports: the reply's run, or a team's
    leader run, whose members' requests each held a context of their own.
    `prices` maps recorded (provider, model ID) identities to prices for the reply's cost.
    """
    metrics = turn_usage(prior_usage, metrics)
    model_config = config.models.get(model_name)
    model_id = model or (model_config.id if model_config is not None else None)
    provider = model_provider or (model_config.provider if model_config is not None else None)
    configured_provider = model_config.provider if model_config is not None else None

    payload: dict[str, Any] = {
        "version": _AI_RUN_METADATA_VERSION,
        "run_id": run_id,
        "session_id": session_id,
        "status": status.value.lower(),
    }
    model_payload: dict[str, Any] = {"config": model_name}
    if model_config is not None and model_config.display_name:
        model_payload["display_name"] = model_config.display_name
    if model_id is not None:
        model_payload["id"] = model_id
    if provider is not None:
        model_payload["provider"] = provider
    payload["model"] = model_payload
    if metrics is not None:
        usage = ReplyUsage.of(
            metrics,
            provider=provider,
            configured_provider=configured_provider,
            model_id=model_id,
        ).to_wire()
        if usage:
            payload["usage"] = usage
    cost = run_cost_usd(
        metrics,
        model_provider=model_provider,
        model_id=model_id,
        prices=prices,
    )
    if cost is not None:
        payload["cost"] = {"usd": format_cost_usd(cost)}
    context = _reply_context_tokens(
        context_run,
        prepared_history,
        provider=provider,
        configured_provider=configured_provider,
        model_id=model_id,
    )
    context_payload = _build_context_payload(context, model_config)
    if context_payload:
        payload["context"] = context_payload
    if prepared_history is not None:
        payload.update(_prepared_history_payload(prepared_history))
    payload["tools"] = {"count": tool_count}

    return {AI_RUN_METADATA_KEY: payload}
