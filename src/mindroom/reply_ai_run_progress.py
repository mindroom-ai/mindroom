"""Live provisional ``io.mindroom.ai_run`` payload builder and per-span progress holder."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from mindroom.ai_run_metadata import ai_run_metrics_fields, prior_usage_by_run
from mindroom.budgets.pricing import format_cost_usd, model_prices
from mindroom.constants import AI_RUN_METADATA_KEY

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.budgets.pricing import PricedModel
    from mindroom.config.main import Config
    from mindroom.delegation.cost import SubagentCost
    from mindroom.history.types import PreparedHistoryState
    from mindroom.llm.messages import Message
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import RunMetrics

_AI_RUN_METADATA_VERSION = 1
_PROVISIONAL_METRIC_KEYS = ("usage", "cost", "context", "tools")


def _copy_provisional_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: dict(value) if isinstance(value, Mapping) else value for key, value in payload.items()}


def build_provisional_ai_run_content(
    *,
    config: Config,
    model_name: str,
    model: str | None = None,
    model_provider: str | None = None,
    answering_model_name: str | None = None,
    prices: Mapping[tuple[str, str], PricedModel] | None = None,
    metrics: RunMetrics | None = None,
    prior_usage: Sequence[RunMetrics] = (),
    messages: Sequence[Message] | None = None,
    prepared_history: PreparedHistoryState | None = None,
    tool_count: int | None = None,
    subagents_usd: float | None = None,
) -> dict[str, Any]:
    """Build provisional, status-less ``io.mindroom.ai_run`` metadata carrying live usage, cost, context, and tools.

    ``subagents_usd`` is passed explicitly rather than read from ContextVars because streaming delivery and terminal
    note writers run outside ``receiving_subagent_cost``.
    """
    payload: dict[str, Any] = {
        "version": _AI_RUN_METADATA_VERSION,
        **ai_run_metrics_fields(
            config=config,
            model_name=model_name,
            model=model,
            model_provider=model_provider,
            answering_model_name=answering_model_name,
            prices=model_prices(config) if prices is None else prices,
            metrics=metrics,
            prior_usage=prior_usage,
            messages=messages,
            prepared_history=prepared_history,
            subagents_usd=subagents_usd,
            include_duration=False,
        ),
    }
    if tool_count is not None and tool_count > 0:
        payload["tools"] = {"count": tool_count}
    return {AI_RUN_METADATA_KEY: payload}


class ReplyAiRunProgress:
    """Live provisional, status-less ``io.mindroom.ai_run`` progress for one reply span."""

    __slots__ = ("_base_payload", "_model", "_subagent_cost", "_version")

    def __init__(self) -> None:
        self._model: dict[str, str] | None = None
        self._base_payload: dict[str, Any] | None = None
        self._subagent_cost: SubagentCost | None = None
        self._version = 0

    def set_model(self, model: Mapping[str, str] | None) -> None:
        """Record the reply's active model payload, clearing attempt-local context and tools when switching models."""
        new_model = dict(model) if model is not None else None
        if new_model == self._model:
            return
        self._model = new_model
        if self._base_payload is not None:
            updated = {
                key: dict(value) if isinstance(value, Mapping) else value
                for key, value in self._base_payload.items()
                if key not in ("context", "tools")
            }
            if new_model is not None:
                updated["model"] = dict(new_model)
            else:
                updated.pop("model", None)
            if updated != self._base_payload:
                self._base_payload = updated
                self._version += 1

    def bind_subagent_cost(self, cost: SubagentCost | None) -> None:
        """Bind the live subagent cost collector whose settled receipts merge into ``cost.subagents_usd``."""
        self._subagent_cost = cost

    def set_prepared_context(
        self,
        config: Config,
        model_name: str,
        prepared_history: PreparedHistoryState | None,
        *,
        prior_usage: Sequence[RunMetrics] = (),
    ) -> None:
        """Seed pre-flight context and prior-attempt spend before an attempt's first request settles."""
        self._store_payload(
            build_provisional_ai_run_content(
                config=config,
                model_name=model_name,
                prior_usage=prior_usage,
                prepared_history=prepared_history,
            )[AI_RUN_METADATA_KEY],
        )

    def update_from_run(
        self,
        config: Config,
        *,
        model_name: str,
        record: AnyRunRecord,
        messages: Sequence[Message],
        prior_usage: Sequence[RunMetrics] = (),
        prepared_history: PreparedHistoryState | None = None,
        metrics_override: RunMetrics | None = None,
        tool_count_override: int | None = None,
    ) -> None:
        """Refresh provisional usage, cost, context, tools, and answering model from a live run."""
        tool_count = len(record.tools or ()) if tool_count_override is None else tool_count_override
        self._store_payload(
            build_provisional_ai_run_content(
                config=config,
                model_name=model_name,
                model=record.model,
                model_provider=record.model_provider,
                answering_model_name=record.model_name,
                metrics=record.metrics if metrics_override is None else metrics_override,
                prior_usage=prior_usage,
                messages=messages,
                prepared_history=prepared_history,
                tool_count=tool_count,
            )[AI_RUN_METADATA_KEY],
        )

    def seed_from_paused(
        self,
        config: Config,
        *,
        model_name: str,
        persisted: AnyRunRecord,
        subagent_cost: SubagentCost | None = None,
    ) -> None:
        """Seed provisional progress from a persisted paused run before resumed progress edits land."""
        if subagent_cost is not None:
            self.bind_subagent_cost(subagent_cost)
        self.update_from_run(
            config,
            model_name=model_name,
            record=persisted,
            messages=persisted.messages,
            prior_usage=tuple(prior_usage_by_run(persisted.metadata).values()),
            prepared_history=None,
        )

    def has_metrics(self) -> bool:
        """Return whether live usage, cost, context, or tool counts are known beyond bare model identity."""
        if self._subagent_cost is not None and self._subagent_cost.usd is not None:
            return True
        if self._base_payload is None:
            return False
        return any(key in self._base_payload for key in _PROVISIONAL_METRIC_KEYS)

    def snapshot_version(self) -> tuple[int, str | None]:
        """Return a version key that advances on both request updates and subagent cost changes."""
        subagents_usd = self._subagent_cost.usd if self._subagent_cost is not None else None
        return (self._version, None if subagents_usd is None else format_cost_usd(subagents_usd))

    def payload(self, *, fallback_model: Mapping[str, str] | None = None) -> dict[str, Any] | None:
        """Return a fresh copy of the status-less ``io.mindroom.ai_run`` dict, or ``None`` if nothing is known."""
        subagents_usd = self._subagent_cost.usd if self._subagent_cost is not None else None
        if self._base_payload is not None:
            result = _copy_provisional_payload(self._base_payload)
            if "model" not in result:
                model = self._model if self._model is not None else fallback_model
                if model is not None:
                    result["model"] = dict(model)
        else:
            model = self._model if self._model is not None else fallback_model
            if model is None and subagents_usd is None:
                return None
            result = {"version": _AI_RUN_METADATA_VERSION}
            if model is not None:
                result["model"] = dict(model)
        if subagents_usd is not None:
            existing_cost = result.get("cost")
            cost = dict(existing_cost) if isinstance(existing_cost, Mapping) else {}
            cost["subagents_usd"] = format_cost_usd(subagents_usd)
            result["cost"] = cost
        return result

    def content(self, *, fallback_model: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Return ``{AI_RUN_METADATA_KEY: payload}`` or ``{}`` when nothing is known."""
        provisional = self.payload(fallback_model=fallback_model)
        return {} if provisional is None else {AI_RUN_METADATA_KEY: provisional}

    def _store_payload(self, payload: dict[str, Any]) -> None:
        model = payload.get("model")
        if isinstance(model, dict):
            self._model = dict(model)
        if payload != self._base_payload:
            self._base_payload = payload
            self._version += 1


__all__ = ["ReplyAiRunProgress", "build_provisional_ai_run_content"]
