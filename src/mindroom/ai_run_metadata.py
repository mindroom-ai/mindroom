"""Build Matrix-visible AI run metadata from model usage counters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.models.metrics import Metrics
from agno.run.base import RunStatus

from mindroom.constants import AI_RUN_METADATA_KEY, ROUTER_AGENT_NAME, RuntimePaths

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.history import PreparedHistoryState

AI_RUN_METADATA_VERSION = 1


def empty_request_metric_totals() -> dict[str, int]:
    """Return zeroed cumulative model-request counters for one streamed run."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def _get_model_config(
    config: Config,
    agent_name: str,
    *,
    runtime_paths: RuntimePaths,
    room_id: str | None = None,
) -> tuple[str | None, ModelConfig | None]:
    """Return configured model name/config for an agent when available."""
    if agent_name not in config.agents and agent_name not in config.teams and agent_name != ROUTER_AGENT_NAME:
        return None, None
    model_name = config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    ).model_name
    return model_name, config.models.get(model_name)


def _serialize_metrics(metrics: Metrics | dict[str, Any] | None) -> dict[str, Any] | None:
    def _sanitize_metrics_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        sanitized: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, (str, int)) or value is None or isinstance(value, bool):
                sanitized[key] = value
            elif isinstance(value, float):
                sanitized[key] = format(value, ".12g")
        return sanitized or None

    if metrics is None:
        return None
    if isinstance(metrics, Metrics):
        metrics_dict = metrics.to_dict()
        if not isinstance(metrics_dict, dict):
            return None
        return _sanitize_metrics_payload(metrics_dict)
    if isinstance(metrics, dict):
        return _sanitize_metrics_payload(metrics)
    return None


def build_model_request_metrics_fallback(
    totals: dict[str, int],
    first_token_latency: float | None,
    observed_fields: set[str] | None = None,
) -> dict[str, Any] | None:
    """Build aggregate usage metadata from streamed request events."""
    if observed_fields is None:
        payload: dict[str, Any] = {key: value for key, value in totals.items() if value > 0}
    else:
        payload: dict[str, Any] = {key: totals[key] for key in observed_fields if key in totals}
    total_tokens = payload.get("total_tokens")
    if isinstance(total_tokens, int) and total_tokens <= 0:
        payload.pop("total_tokens")
    if "total_tokens" not in payload:
        input_tokens = payload.get("input_tokens")
        output_tokens = payload.get("output_tokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            payload["total_tokens"] = input_tokens + output_tokens
    if first_token_latency is not None:
        payload["time_to_first_token"] = format(first_token_latency, ".12g")
    return payload or None


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
    if cache_read_tokens is not None and cache_read_tokens > 0:
        payload["cache_read_input_tokens"] = cache_read_tokens
    if cache_write_tokens is not None and cache_write_tokens > 0:
        payload["cache_write_input_tokens"] = cache_write_tokens
    if cache_read_tokens is not None or cache_write_tokens is not None:
        # Cache writes were not read from cache, so they remain in the non-cache-read bucket.
        uncached_input_tokens = context_input_tokens - (cache_read_tokens or 0)
        if uncached_input_tokens >= 0:
            payload["uncached_input_tokens"] = uncached_input_tokens
    return payload


def _provider_reports_cache_tokens_outside_input(
    *,
    provider: str | None,
    configured_provider: str | None,
    model_id: str | None,
) -> bool:
    """Return whether cache tokens must be added to input tokens for context occupancy."""
    provider_key = (provider or configured_provider or "").lower()
    configured_provider_key = (configured_provider or "").lower()
    model_key = (model_id or "").lower()
    if "anthropic" in provider_key or "bedrock" in provider_key:
        return True
    if configured_provider_key == "vertexai_claude":
        return True
    return "vertex" in provider_key and "claude" in model_key


def _context_input_tokens_from_counts(
    *,
    input_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    provider: str | None,
    configured_provider: str | None,
    model_id: str | None,
) -> int | None:
    """Return full request-context tokens from provider usage counters."""
    if input_tokens is None:
        return None
    if not _provider_reports_cache_tokens_outside_input(
        provider=provider,
        configured_provider=configured_provider,
        model_id=model_id,
    ):
        return input_tokens
    return input_tokens + (cache_read_tokens or 0) + (cache_write_tokens or 0)


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
    payload: dict[str, Any] = {"version": AI_RUN_METADATA_VERSION}
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


def build_ai_run_metadata_content(  # noqa: C901, PLR0912, PLR0915
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    run_id: str | None,
    session_id: str | None,
    status: RunStatus | str | None,
    model: str | None,
    model_provider: str | None,
    room_id: str | None = None,
    metrics: Metrics | dict[str, Any] | None = None,
    metrics_fallback: dict[str, Any] | None = None,
    context_raw_input_tokens: int | None = None,
    context_input_tokens: int | None = None,
    context_cache_read_tokens: int | None = None,
    context_cache_write_tokens: int | None = None,
    tool_count: int | None = None,
    prepared_history: PreparedHistoryState | None = None,
) -> dict[str, Any] | None:
    """Build the Matrix event content fragment for one AI run."""
    model_name, model_config = _get_model_config(
        config,
        agent_name,
        runtime_paths=runtime_paths,
        room_id=room_id,
    )
    model_id = model or (model_config.id if model_config is not None else None)
    provider = model_provider or (model_config.provider if model_config is not None else None)

    usage_payload = _serialize_metrics(metrics)
    if metrics_fallback:
        fallback_usage_payload = dict(metrics_fallback)
        if usage_payload is None:
            usage_payload = fallback_usage_payload
        else:
            for key, value in fallback_usage_payload.items():
                usage_payload.setdefault(key, value)

    usage_input_tokens = usage_payload.get("input_tokens") if usage_payload else None
    if not isinstance(usage_input_tokens, int):
        usage_input_tokens = None
    explicit_context_scope = any(
        value is not None
        for value in (
            context_raw_input_tokens,
            context_input_tokens,
            context_cache_read_tokens,
            context_cache_write_tokens,
        )
    )
    resolved_context_input_tokens = context_input_tokens
    if resolved_context_input_tokens is None:
        resolved_context_input_tokens = _context_input_tokens_from_counts(
            input_tokens=context_raw_input_tokens if explicit_context_scope else usage_input_tokens,
            cache_read_tokens=(
                context_cache_read_tokens
                if explicit_context_scope
                else _int_usage_value(usage_payload, "cache_read_tokens")
            ),
            cache_write_tokens=(
                context_cache_write_tokens
                if explicit_context_scope
                else _int_usage_value(usage_payload, "cache_write_tokens")
            ),
            provider=provider,
            configured_provider=model_config.provider if model_config is not None else None,
            model_id=model_id,
        )
    resolved_context_cache_read_tokens = context_cache_read_tokens
    if resolved_context_cache_read_tokens is None and not explicit_context_scope:
        resolved_context_cache_read_tokens = _int_usage_value(usage_payload, "cache_read_tokens")
    resolved_context_cache_write_tokens = context_cache_write_tokens
    if resolved_context_cache_write_tokens is None and not explicit_context_scope:
        resolved_context_cache_write_tokens = _int_usage_value(usage_payload, "cache_write_tokens")

    payload: dict[str, Any] = {"version": AI_RUN_METADATA_VERSION}
    if run_id is not None:
        payload["run_id"] = run_id
    if session_id is not None:
        payload["session_id"] = session_id
    if status is not None:
        raw_status = status.value if isinstance(status, RunStatus) else str(status)
        payload["status"] = raw_status.lower()
    if model_name is not None or model_id is not None or provider is not None:
        model_payload: dict[str, Any] = {}
        if model_name is not None:
            model_payload["config"] = model_name
        if model_id is not None:
            model_payload["id"] = model_id
        if provider is not None:
            model_payload["provider"] = provider
        if model_payload:
            payload["model"] = model_payload
    if usage_payload:
        payload["usage"] = usage_payload
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

    if len(payload) == 1:
        return None
    return {AI_RUN_METADATA_KEY: payload}
