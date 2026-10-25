"""Build Matrix-visible AI run metadata from model usage counters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.budgets.pricing import format_cost_usd, run_cost_usd
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.llm.runs import RunStatus
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.model_usage import context_input_tokens_from_counts, uncached_input_tokens_from_counts
from mindroom.providers.names import canonical_provider

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.budgets.pricing import PricedModel
    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.history.types import PreparedHistoryState

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


def _build_context_payload(
    *,
    context_input_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    model_config: ModelConfig | None,
) -> dict[str, Any] | None:
    if context_input_tokens is None or model_config is None or model_config.context_window is None:
        return None
    context_window = model_config.context_window
    if context_window <= 0:
        return None
    payload = {
        "input_tokens": context_input_tokens,
        "window_tokens": context_window,
    }
    bounded_cache_read_tokens: int | None = None
    if cache_read_tokens is not None and cache_read_tokens > 0:
        bounded_cache_read_tokens = min(cache_read_tokens, context_input_tokens)
        if bounded_cache_read_tokens > 0:
            payload["cache_read_input_tokens"] = bounded_cache_read_tokens
    uncached_input_tokens: int | None = None
    if cache_read_tokens is not None or cache_write_tokens is not None:
        # Cache writes were not read from cache, so they remain in the non-cache-read bucket.
        uncached_input_tokens = context_input_tokens - (bounded_cache_read_tokens or 0)
        if uncached_input_tokens >= 0:
            payload["uncached_input_tokens"] = uncached_input_tokens
    if (
        cache_write_tokens is not None
        and cache_write_tokens > 0
        and uncached_input_tokens is not None
        and cache_write_tokens <= uncached_input_tokens
    ):
        payload["cache_write_input_tokens"] = cache_write_tokens
    return payload


def _int_usage_value(usage_payload: dict[str, Any] | None, key: str) -> int | None:
    if usage_payload is None:
        return None
    value = usage_payload.get(key)
    return value if isinstance(value, int) else None


def _build_compaction_metadata_payload(prepared_history: PreparedHistoryState | None) -> dict[str, Any] | None:
    """Serialize reply-level compaction diagnostics for Matrix run metadata."""
    if prepared_history is None:
        return None
    decision = prepared_history.compaction_decision
    payload: dict[str, Any] = {
        "decision": decision.mode,
        "outcome": prepared_history.compaction_reply_outcome,
        "reason": decision.reason,
    }
    if decision.current_history_tokens is not None:
        payload["current_history_tokens"] = decision.current_history_tokens
    if decision.trigger_budget_tokens is not None:
        payload["trigger_budget_tokens"] = decision.trigger_budget_tokens
    if decision.hard_budget_tokens is not None:
        payload["hard_budget_tokens"] = decision.hard_budget_tokens
    if decision.fitted_replay_tokens is not None:
        payload["fitted_replay_tokens"] = decision.fitted_replay_tokens
    if prepared_history.replay_plan is not None:
        payload["replay_plan"] = {
            "mode": prepared_history.replay_plan.mode,
            "estimated_tokens": prepared_history.replay_plan.estimated_tokens,
        }
    return payload


def build_prepared_history_metadata_content(prepared_history: PreparedHistoryState | None) -> dict[str, Any] | None:
    """Build Matrix message metadata for prepared-context and compaction diagnostics."""
    if prepared_history is None:
        return None
    payload: dict[str, Any] = {"version": _AI_RUN_METADATA_VERSION}
    if prepared_history.prepared_context_tokens is not None:
        payload["prepared_context"] = {
            "tokens": prepared_history.prepared_context_tokens,
        }
    compaction_payload = _build_compaction_metadata_payload(prepared_history)
    if compaction_payload:
        payload["compaction"] = compaction_payload
    if len(payload) == 1:
        return None
    return {AI_RUN_METADATA_KEY: payload}


def ai_run_extra_content_from_metadata(run_metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the Matrix-visible AI run metadata subset from persisted run metadata."""
    if run_metadata is None:
        return None
    ai_run_metadata = run_metadata.get(AI_RUN_METADATA_KEY)
    if not isinstance(ai_run_metadata, dict):
        return None
    return {AI_RUN_METADATA_KEY: dict(ai_run_metadata)}


def _context_provider_key(provider: str | None) -> str:
    key = canonical_provider(provider or "")
    return {"vertexai_claude": "vertexai", "bedrock_claude": "awsbedrock"}.get(key, key)


def _native_context_counts(
    metrics: RunMetrics | None,
    model_id: str | None,
    provider: str | None,
) -> tuple[int | None, int | None, int | None] | None:
    """Read final-iteration context separately from accumulated billing."""
    if isinstance(metrics, RunMetrics):
        for detail in (metrics.details or {}).get("model", []):
            if detail.id == model_id and _context_provider_key(detail.provider) == _context_provider_key(provider):
                usage = (detail.provider_metrics or {}).get("context_usage")
                if isinstance(usage, dict):
                    return (
                        _int_usage_value(usage, "input_tokens"),
                        _int_usage_value(usage, "cache_read_tokens"),
                        _int_usage_value(usage, "cache_write_tokens"),
                    )
    return None


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


def build_ai_run_metadata_content(  # noqa: C901, PLR0912, PLR0915
    *,
    config: Config,
    model_name: str,
    run_id: str | None,
    session_id: str | None,
    status: RunStatus | str | None,
    model: str | None,
    model_provider: str | None,
    prices: Mapping[tuple[str, str], PricedModel],
    metrics: RunMetrics | None = None,
    context_metrics: RunMetrics | None = None,
    context_raw_input_tokens: int | None = None,
    context_input_tokens: int | None = None,
    context_cache_read_tokens: int | None = None,
    context_cache_write_tokens: int | None = None,
    tool_count: int | None = None,
    prepared_history: PreparedHistoryState | None = None,
) -> dict[str, Any]:
    """Build the Matrix event content fragment for one AI run.

    `model_name` is the configured model name resolved at run preparation time.
    It must not be re-resolved here: the per-thread override store can change
    mid-run (for example via `switch_thread_model`), and this metadata must
    describe the model that actually produced the response.
    `context_metrics` belongs to that model run before team-member billing is aggregated.
    `prices` maps recorded (provider, model ID) identities to prices for the reply's cost.
    """
    model_config = config.models.get(model_name)
    model_id = model or (model_config.id if model_config is not None else None)
    provider = model_provider or (model_config.provider if model_config is not None else None)

    # The reply's counters, a zero one read as unreported, as the published usage leaves it out.
    tokens = metrics.tokens if metrics is not None else TokenCounts()
    usage_input_tokens = tokens.input_tokens or None
    # Native compaction adds a billed sampling iteration. The reply's usage and cost
    # include it, while the final iteration alone describes the active context.
    if native_counts := _native_context_counts(context_metrics, model_id, provider):
        context_raw_input_tokens, context_cache_read_tokens, context_cache_write_tokens = native_counts
    explicit_context_scope = any(
        value is not None
        for value in (
            context_raw_input_tokens,
            context_cache_read_tokens,
            context_cache_write_tokens,
        )
    )
    # Provider usage counters are authoritative for context occupancy; the
    # pre-flight `context_input_tokens` estimate is only a fallback when the
    # provider reported nothing (for example a request that failed early).
    resolved_context_input_tokens = context_input_tokens_from_counts(
        input_tokens=context_raw_input_tokens if explicit_context_scope else usage_input_tokens,
        cache_read_tokens=context_cache_read_tokens if explicit_context_scope else tokens.cache_read_tokens or None,
        cache_write_tokens=context_cache_write_tokens if explicit_context_scope else tokens.cache_write_tokens or None,
        provider=provider,
        configured_provider=model_config.provider if model_config is not None else None,
        model_id=model_id,
    )
    if resolved_context_input_tokens is None:
        resolved_context_input_tokens = context_input_tokens
    resolved_context_cache_read_tokens = context_cache_read_tokens
    if resolved_context_cache_read_tokens is None and not explicit_context_scope:
        resolved_context_cache_read_tokens = tokens.cache_read_tokens or None
    resolved_context_cache_write_tokens = context_cache_write_tokens
    if resolved_context_cache_write_tokens is None and not explicit_context_scope:
        resolved_context_cache_write_tokens = tokens.cache_write_tokens or None

    payload: dict[str, Any] = {"version": _AI_RUN_METADATA_VERSION}
    if run_id is not None:
        payload["run_id"] = run_id
    if session_id is not None:
        payload["session_id"] = session_id
    if status is not None:
        raw_status = status.value if isinstance(status, RunStatus) else str(status)
        payload["status"] = raw_status.lower()
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
            configured_provider=model_config.provider if model_config is not None else None,
            model_id=model_id,
        )
        if usage := _ReplyUsage.of(metrics, uncached_input_tokens=uncached_input_tokens).to_wire():
            payload["usage"] = usage
    cost = run_cost_usd(
        metrics,
        tokens.to_dict() if metrics is not None else None,
        model_provider=model_provider,
        model_id=model_id,
        prices=prices,
    )
    if cost is not None:
        payload["cost"] = {"usd": format_cost_usd(cost)}
    context_payload = _build_context_payload(
        context_input_tokens=resolved_context_input_tokens,
        cache_read_tokens=resolved_context_cache_read_tokens,
        cache_write_tokens=resolved_context_cache_write_tokens,
        model_config=model_config,
    )
    if context_payload:
        payload["context"] = context_payload
    if prepared_history is not None and prepared_history.prepared_context_tokens is not None:
        payload["prepared_context"] = {
            "tokens": prepared_history.prepared_context_tokens,
        }
    compaction_payload = _build_compaction_metadata_payload(prepared_history)
    if compaction_payload:
        payload["compaction"] = compaction_payload
    if tool_count is not None:
        payload["tools"] = {"count": tool_count}

    return {AI_RUN_METADATA_KEY: payload}
