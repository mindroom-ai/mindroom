# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Token usage and timing metrics, with the dict shapes stored in runs, usage rows, and ``io.mindroom.ai_run``.

``to_dict`` omits None, zero integers, and empty containers exactly as Agno 3 did, because
``usage/storage.py``, the budget code, and MindRoom Chat read these keys.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_TOKEN_FIELDS = (
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


type _Counted = MessageMetrics | ModelMetrics | RunMetrics


def _add_token_counts(target: _Counted, source: _Counted) -> None:
    """Add every token counter of ``source`` into ``target`` in place."""
    for name in _TOKEN_FIELDS:
        setattr(target, name, getattr(target, name) + getattr(source, name))


def _compact(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop None, zero integers, and empty dicts or lists."""
    return {
        key: value
        for key, value in values.items()
        if value is not None
        and (not isinstance(value, int) or value != 0)
        and (not isinstance(value, (dict, list)) or len(value) > 0)
    }


def _known(cls: type, data: Mapping[str, Any]) -> dict[str, Any]:
    names = {item.name for item in fields(cls)}
    return {key: value for key, value in data.items() if key in names}


def _sum_optional(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _merge_numeric(target: dict[str, Any], source: Mapping[str, Any] | None) -> None:
    """Add numeric values key by key and keep the latest value for anything else."""
    if not source:
        return
    for key, value in source.items():
        current = target.get(key)
        if isinstance(value, (int, float)) and isinstance(current, (int, float)):
            target[key] = current + value
        else:
            target[key] = value


@dataclass(slots=True, kw_only=True)
class MessageMetrics:
    """Usage one provider request reported; stored on the assistant message."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0
    audio_total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float | None = None
    duration: float | None = None
    time_to_first_token: float | None = None
    provider_metrics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict; empty when nothing was reported."""
        return _compact(asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MessageMetrics:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        return cls(**_known(cls, data))

    def __add__(self, other: MessageMetrics) -> MessageMetrics:
        """Sum counters, cost, and duration; keep the first time to first token."""
        result = MessageMetrics(
            cost=_sum_optional(self.cost, other.cost),
            duration=_sum_optional(self.duration, other.duration),
            time_to_first_token=(
                self.time_to_first_token if self.time_to_first_token is not None else other.time_to_first_token
            ),
        )
        _add_token_counts(result, self)
        _add_token_counts(result, other)
        if self.provider_metrics is not None or other.provider_metrics is not None:
            merged: dict[str, Any] = dict(self.provider_metrics or {})
            _merge_numeric(merged, other.provider_metrics)
            result.provider_metrics = merged
        return result


@dataclass(slots=True, kw_only=True)
class ModelMetrics:
    """Usage one model accumulated inside a run; an entry of ``RunMetrics.details``."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0
    audio_total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float | None = None
    id: str = ""
    provider: str = ""
    provider_metrics: dict[str, Any] | None = None

    def accumulate(self, other: ModelMetrics | MessageMetrics) -> None:
        """Add another report for the same model into this entry."""
        _add_token_counts(self, other)
        if other.cost is not None:
            self.cost = (self.cost or 0) + other.cost
        if other.provider_metrics is not None:
            if self.provider_metrics is None:
                self.provider_metrics = {}
            _merge_numeric(self.provider_metrics, other.provider_metrics)

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict shape."""
        return _compact(asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelMetrics:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        return cls(**_known(cls, data))


def _details_to_dict(details: Mapping[str, list[ModelMetrics]]) -> dict[str, list[dict[str, Any]]]:
    # Agno kept empty ``provider_metrics`` dicts inside details, unlike ``ModelMetrics.to_dict``.
    return {
        model_type: [
            {
                key: value
                for key, value in asdict(entry).items()
                if value is not None and (not isinstance(value, int) or value != 0)
            }
            for entry in entries
        ]
        for model_type, entries in details.items()
    }


def _details_from_dict(details: Mapping[str, list[Any]]) -> dict[str, list[ModelMetrics]]:
    return {
        model_type: [ModelMetrics.from_dict(entry) if isinstance(entry, dict) else entry for entry in entries]
        for model_type, entries in details.items()
    }


@dataclass(slots=True, kw_only=True)
class RunMetrics:
    """Usage one run accumulated over all its requests, with a per-model breakdown in ``details``."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0
    audio_total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float | None = None
    time_to_first_token: float | None = None
    duration: float | None = None
    details: dict[str, list[ModelMetrics]] | None = None
    additional_metrics: dict[str, Any] | None = None

    def add(self, usage: MessageMetrics, *, model_id: str, provider: str) -> None:
        """Count one request's usage under ``details["model"]`` for ``(provider, model_id)`` and in the totals.

        ``usage`` is left untouched: the entry copies ``provider_metrics`` so later requests never rewrite the
        counters stored on an earlier assistant message.
        """
        entry = ModelMetrics(
            cost=usage.cost,
            id=model_id,
            provider=provider,
            provider_metrics=None if usage.provider_metrics is None else dict(usage.provider_metrics),
        )
        _add_token_counts(entry, usage)
        if self.details is None:
            self.details = {}
        entries = self.details.setdefault("model", [])
        for existing in entries:
            if existing.id == model_id and existing.provider == provider:
                existing.accumulate(entry)
                break
        else:
            entries.append(entry)
        _add_token_counts(self, usage)
        if usage.cost is not None:
            self.cost = (self.cost or 0) + usage.cost

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict shape, which is also the ``io.mindroom.ai_run`` usage payload."""
        values = asdict(self)
        if self.details is not None:
            values["details"] = _details_to_dict(self.details)
        return _compact(values)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunMetrics:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        values = _known(cls, data)
        if values.get("details") is not None:
            values["details"] = _details_from_dict(values["details"])
        return cls(**values)

    def __add__(self, other: RunMetrics) -> RunMetrics:
        """Sum two runs' usage, merging ``details`` by model type, provider, and id."""
        result = RunMetrics(
            cost=_sum_optional(self.cost, other.cost),
            duration=_sum_optional(self.duration, other.duration),
        )
        _add_token_counts(result, self)
        _add_token_counts(result, other)
        first_tokens = [t for t in (self.time_to_first_token, other.time_to_first_token) if t is not None]
        if first_tokens:
            result.time_to_first_token = min(first_tokens)
        if self.details or other.details:
            merged: dict[str, dict[tuple[str, str], ModelMetrics]] = {}
            for details in (self.details, other.details):
                for model_type, entries in (details or {}).items():
                    by_model = merged.setdefault(model_type, {})
                    for entry in entries:
                        key = (entry.provider, entry.id)
                        if key in by_model:
                            by_model[key].accumulate(entry)
                        else:
                            by_model[key] = ModelMetrics.from_dict(entry.to_dict())
            result.details = {model_type: list(by_model.values()) for model_type, by_model in merged.items()}
        if self.additional_metrics is not None or other.additional_metrics is not None:
            additional: dict[str, Any] = dict(self.additional_metrics or {})
            _merge_numeric(additional, other.additional_metrics)
            result.additional_metrics = additional
        return result


@dataclass(slots=True, kw_only=True)
class ToolCallMetrics:
    """Wall-clock timing of one tool call."""

    start_time: float | None = None
    end_time: float | None = None
    duration: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict shape."""
        return _compact(asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolCallMetrics:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        return cls(**_known(cls, data))


__all__ = [
    "MessageMetrics",
    "ModelMetrics",
    "RunMetrics",
    "ToolCallMetrics",
]
