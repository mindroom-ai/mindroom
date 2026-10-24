# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Token usage and timing metrics, with the dict shapes stored in runs, session rows, and usage rows.

``to_dict`` omits None, zero integers, and empty containers: the stored shape that ``usage/storage.py`` and the budget
code read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenCounts:
    """The token counters a request, a run, or a usage report holds, in the order usage rows and reports list them."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0
    audio_total_tokens: int = 0

    @classmethod
    def from_mapping(cls, values: Mapping[str, object], read: Callable[[object], int]) -> TokenCounts:
        """Read each counter of a stored dict, None when it is missing, through the reader's own validation rule."""
        return cls(**{name: read(values.get(name)) for name in TOKEN_FIELDS})

    def with_totals(self) -> TokenCounts:
        """Return these counts with ``total_tokens`` and ``audio_total_tokens`` the sums of their parts."""
        return replace(
            self,
            total_tokens=self.input_tokens + self.output_tokens,
            audio_total_tokens=self.audio_input_tokens + self.audio_output_tokens,
        )

    def to_dict(self, *, first: tuple[str, ...] = ()) -> dict[str, int]:
        """Return every counter, zero included: the ``first`` names in that order, then the rest in report order."""
        counters = asdict(self)
        return {**{name: counters.pop(name) for name in first}, **counters}

    def __add__(self, other: TokenCounts) -> TokenCounts:
        """Add the counters one by one."""
        return TokenCounts(**{name: getattr(self, name) + getattr(other, name) for name in TOKEN_FIELDS})

    def __sub__(self, other: TokenCounts) -> TokenCounts:
        """Subtract the counters one by one."""
        return TokenCounts(**{name: getattr(self, name) - getattr(other, name) for name in TOKEN_FIELDS})


TOKEN_FIELDS = tuple(item.name for item in fields(TokenCounts))
# Stored usage dicts list the counters first, in this order, as main stored them; a counter it omits follows them.
_STORED_COUNTER_ORDER = (
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


def _compact(values: Mapping[str, Any], *, keep_empty: bool = False) -> dict[str, Any]:
    """Drop None and zero integers, and empty dicts or lists unless ``keep_empty``."""
    return {
        key: value
        for key, value in values.items()
        if value is not None
        and (not isinstance(value, int) or value != 0)
        and (keep_empty or not isinstance(value, (dict, list)) or len(value) > 0)
    }


def _known_fields(cls: type, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the entries of a stored dict that name a field of the dataclass ``cls``."""
    names = {item.name for item in fields(cls)}
    return {key: value for key, value in data.items() if key in names}


def _flattened(record: _Counts) -> dict[str, Any]:
    """Return a usage record's fields with its ``tokens`` spread out first, in stored order."""
    values = asdict(record)
    del values["tokens"]
    return {**record.tokens.to_dict(first=_STORED_COUNTER_ORDER), **values}


def _known_counts(cls: type, data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the entries of a stored usage dict that name a field of ``cls``, its counters read into ``tokens``."""
    return {**_known_fields(cls, data), "tokens": TokenCounts(**_known_fields(TokenCounts, data))}


@dataclass(slots=True, kw_only=True)
class _Counts:
    """The token counters and cost every usage record carries, first in its stored dict."""

    tokens: TokenCounts = TokenCounts()
    cost: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict shape."""
        return _compact(_flattened(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        return cls(**_known_counts(cls, data))


def _add_counts(target: _Counts, source: _Counts, *, sign: int = 1) -> None:
    """Add the token counters and any cost of ``source``, times ``sign``, into ``target`` in place."""
    target.tokens = target.tokens + source.tokens if sign > 0 else target.tokens - source.tokens
    if source.cost is not None:
        target.cost = (target.cost or 0) + sign * source.cost


def _sum_optional(left: float | None, right: float | None) -> float | None:
    # TODO(simplify): costs sum three ways: ``__add__`` by this rule, ``_add_counts`` with a 0.0 total read as unset,
    # and ``_signed_model`` by sign, so sums differ in int or float type and a zero's sign. Sum every cost one way.
    # Stored data would notice only in those edge cases.
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
class MessageMetrics(_Counts):
    """Usage one provider request reported; stored on the assistant message."""

    duration: float | None = None
    time_to_first_token: float | None = None
    provider_metrics: dict[str, Any] | None = None

    def __add__(self, other: MessageMetrics) -> MessageMetrics:
        """Sum counters, cost, and duration; keep the first time to first token."""
        result = MessageMetrics(
            tokens=self.tokens + other.tokens,
            cost=_sum_optional(self.cost, other.cost),
            duration=_sum_optional(self.duration, other.duration),
            time_to_first_token=(
                self.time_to_first_token if self.time_to_first_token is not None else other.time_to_first_token
            ),
        )
        if self.provider_metrics is not None or other.provider_metrics is not None:
            merged: dict[str, Any] = dict(self.provider_metrics or {})
            _merge_numeric(merged, other.provider_metrics)
            result.provider_metrics = merged
        return result


def add_usage(total: MessageMetrics | None, usage: MessageMetrics | None) -> MessageMetrics | None:
    """Return the sum of two optional usage reports."""
    if total is None or usage is None:
        return usage if total is None else total
    return total + usage


@dataclass(slots=True, kw_only=True)
class ModelMetrics(_Counts):
    """Usage one model accumulated inside a run; an entry of ``RunMetrics.details``."""

    id: str = ""
    provider: str = ""
    provider_metrics: dict[str, Any] | None = None

    def accumulate(self, other: ModelMetrics | MessageMetrics) -> None:
        """Add another report for the same model into this entry."""
        _add_counts(self, other)
        if other.provider_metrics is not None:
            if self.provider_metrics is None:
                self.provider_metrics = {}
            _merge_numeric(self.provider_metrics, other.provider_metrics)


def _details_to_dict(details: Mapping[str, list[ModelMetrics]]) -> dict[str, list[dict[str, Any]]]:
    # TODO(simplify): details entries keep an empty ``provider_metrics`` that ``ModelMetrics.to_dict`` drops.
    # Write each entry with ``to_dict``. Stored data would notice: details entries lose ``"provider_metrics": {}``.
    return {
        model_type: [_compact(_flattened(entry), keep_empty=True) for entry in entries]
        for model_type, entries in details.items()
    }


def _details_from_dict(details: Mapping[str, list[Any]]) -> dict[str, list[ModelMetrics]]:
    return {
        model_type: [ModelMetrics.from_dict(entry) if isinstance(entry, dict) else entry for entry in entries]
        for model_type, entries in details.items()
    }


@dataclass(slots=True, kw_only=True)
class RunMetrics(_Counts):
    """Usage one run accumulated over all its requests, with a per-model breakdown in ``details``."""

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
            tokens=usage.tokens,
            cost=usage.cost,
            id=model_id,
            provider=provider,
            provider_metrics=None if usage.provider_metrics is None else dict(usage.provider_metrics),
        )
        if self.details is None:
            self.details = {}
        entries = self.details.setdefault("model", [])
        for existing in entries:
            if existing.id == model_id and existing.provider == provider:
                existing.accumulate(entry)
                break
        else:
            entries.append(entry)
        _add_counts(self, usage)

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict shape."""
        values = _flattened(self)
        if self.details is not None:
            values["details"] = _details_to_dict(self.details)
        return _compact(values)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        values = _known_counts(cls, data)
        if values.get("details") is not None:
            values["details"] = _details_from_dict(values["details"])
        return cls(**values)

    def __add__(self, other: RunMetrics) -> RunMetrics:
        """Sum two runs' usage, merging ``details`` by model type, provider, and id."""
        result = RunMetrics(
            tokens=self.tokens + other.tokens,
            cost=_sum_optional(self.cost, other.cost),
            duration=_sum_optional(self.duration, other.duration),
        )
        first_tokens = [t for t in (self.time_to_first_token, other.time_to_first_token) if t is not None]
        if first_tokens:
            result.time_to_first_token = min(first_tokens)
        if self.details or other.details:
            result.details = {}
            for details in (self.details, other.details):
                for model_type, entries in (details or {}).items():
                    totals = result.details.setdefault(model_type, [])
                    for entry in entries:
                        _count_model(totals, entry)
        if self.additional_metrics is not None or other.additional_metrics is not None:
            additional: dict[str, Any] = dict(self.additional_metrics or {})
            _merge_numeric(additional, other.additional_metrics)
            result.additional_metrics = additional
        return result


@dataclass(slots=True, kw_only=True)
class SessionMetrics(_Counts):
    """Usage every run of a conversation added up; stored as ``session_data["session_metrics"]`` on the session row."""

    details: dict[str, list[ModelMetrics]] | None = None
    additional_metrics: dict[str, Any] | None = None

    def count_run(self, metrics: RunMetrics, *, replacing: RunMetrics | None = None) -> None:
        """Add one run's usage, first taking back ``replacing``, what an earlier save of the same run added."""
        if replacing is not None:
            self._accumulate(replacing, sign=-1)
        self._accumulate(metrics, sign=1)

    def _accumulate(self, metrics: RunMetrics, *, sign: int) -> None:
        # TODO(simplify): the session ledger passes token counters only, so the cost, additional_metrics, and
        # provider_metrics handling here and in ``_signed_model`` serves only tests/llm/test_codecs.py's full-metrics
        # cases. Count tokens only; only those tests and their goldens would notice.
        _add_counts(self, metrics, sign=sign)
        if metrics.additional_metrics is not None:
            if self.additional_metrics is None:
                self.additional_metrics = {}
            _merge_numeric(self.additional_metrics, _scaled(metrics.additional_metrics, sign))
        for model_type, entries in (metrics.details or {}).items():
            if self.details is None:
                self.details = {}
            totals = self.details.setdefault(model_type, [])
            for entry in entries:
                _count_model(totals, _signed_model(entry, sign))

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict shape."""
        values = _flattened(self)
        if self.details is not None:
            values["details"] = _details_to_dict(self.details)
        return _compact(values)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild from a stored dict, ignoring keys this type does not model and a ``details`` that is not a mapping."""
        values = _known_counts(cls, data)
        details = values.pop("details", None)
        if isinstance(details, dict):
            values["details"] = _details_from_dict(
                {model_type: entries for model_type, entries in details.items() if isinstance(entries, list)},
            )
        return cls(**values)


def _signed_model(entry: ModelMetrics, sign: int) -> ModelMetrics:
    return ModelMetrics(
        tokens=entry.tokens if sign > 0 else TokenCounts() - entry.tokens,
        cost=None if entry.cost is None else sign * entry.cost,
        id=entry.id,
        provider=entry.provider,
        provider_metrics=None if entry.provider_metrics is None else _scaled(entry.provider_metrics, sign),
    )


def _count_model(totals: list[ModelMetrics], entry: ModelMetrics) -> None:
    """Add ``entry`` to the total of its ``(provider, id)``, or start one from a copy."""
    for total in totals:
        if (total.provider, total.id) == (entry.provider, entry.id):
            total.accumulate(entry)
            return
    # The copy drops an empty ``provider_metrics``.
    totals.append(ModelMetrics.from_dict(entry.to_dict()))


def _scaled(values: Mapping[str, Any], sign: int) -> dict[str, Any]:
    """Return ``values`` with numbers multiplied by ``sign``; other values are kept as they are."""
    return {
        key: sign * value if isinstance(value, (int, float)) and not isinstance(value, bool) else value
        for key, value in values.items()
    }


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
        return cls(**_known_fields(cls, data))


__all__ = [
    "TOKEN_FIELDS",
    "MessageMetrics",
    "ModelMetrics",
    "RunMetrics",
    "SessionMetrics",
    "TokenCounts",
    "ToolCallMetrics",
    "add_usage",
]
