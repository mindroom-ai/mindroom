"""Build Matrix-visible AI run metadata from model usage counters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.budgets.pricing import format_cost_usd, run_cost_usd
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.llm.messages import is_own_model_message
from mindroom.model_usage import ContextTokens, message_context_tokens, uncached_input_tokens_from_counts

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.budgets.pricing import PricedModel
    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.history.types import PreparedHistoryState
    from mindroom.llm.runs import AnyRunRecord, RunStatus
    from mindroom.llm.usage import RunMetrics, TokenCounts

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
class _ReplyUsage:
    """The ``usage`` object of a reply's ``io.mindroom.ai_run`` metadata, kept apart from how runs store usage.

    Zero counters and unset values are left out, and fractions are published as decimal strings, because Matrix
    event JSON forbids floats.
    """

    tokens: TokenCounts
    cost: float | None
    time_to_first_token: float | None
    duration: float | None
    uncached_input_tokens: int | None

    @classmethod
    def of(cls, metrics: RunMetrics, *, uncached_input_tokens: int | None) -> _ReplyUsage:
        """Return the usage a run's metrics publish."""
        return cls(metrics.tokens, metrics.cost, metrics.time_to_first_token, metrics.duration, uncached_input_tokens)

    def to_wire(self) -> dict[str, Any]:
        """Return the published object, empty when nothing was reported; the uncached input is published as given."""
        values = {
            **self.tokens.to_dict(first=_PUBLISHED_COUNTERS),
            "cost": self.cost,
            "time_to_first_token": self.time_to_first_token,
            "duration": self.duration,
        }
        wire: dict[str, Any] = {
            key: format(value, ".12g") if isinstance(value, float) else value
            for key, value in values.items()
            if isinstance(value, (float, str)) or (isinstance(value, int) and value != 0)
        }
        if wire and self.uncached_input_tokens is not None:
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
    context_run: AnyRunRecord | None = None,
    prepared_history: PreparedHistoryState | None = None,
) -> dict[str, Any]:
    """Build the Matrix event content fragment for one AI run.

    `model_name` is the configured model name resolved at run preparation time.
    It must not be re-resolved here: the per-thread override store can change
    mid-run (for example via `switch_thread_model`), and this metadata must
    describe the model that actually produced the response.
    `context_run` is the run whose last own model request's context the reply reports: the reply's run, or a team's
    leader run, whose members' requests each held a context of their own.
    `prices` maps recorded (provider, model ID) identities to prices for the reply's cost.
    """
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
        uncached_input_tokens = _uncached_input_tokens(
            metrics,
            provider=provider,
            configured_provider=configured_provider,
            model_id=model_id,
        )
        if usage := _ReplyUsage.of(metrics, uncached_input_tokens=uncached_input_tokens).to_wire():
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
