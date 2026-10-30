# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Token usage and timing metrics, with the dict shapes stored in runs and usage rows.

``to_dict`` omits None, zero integers, and empty containers: the stored shape that ``usage/storage.py`` and the budget
code read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from time import time
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.llm.messages import ModelProvenance


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
# Stored usage dicts list the counters first, in this order, so every stored row keeps one key order; a counter it
# omits follows them.
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


def _add_counts(target: _Counts, source: _Counts) -> None:
    """Add the token counters and any cost of ``source`` into ``target`` in place."""
    target.tokens = target.tokens + source.tokens
    target.cost = _sum_optional(target.cost, source.cost)


def _sum_optional(left: float | None, right: float | None) -> float | None:
    """Add two amounts a usage report may leave unset, such as costs and durations; None when neither is set."""
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
    """Usage one provider request reported; stored on the assistant message.

    ``duration`` is the finishing attempt's: a retried request's counters also hold the tokens its failed attempts
    reported, but its duration times only the attempt that finished.
    """

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

    def accumulate(self, other: ModelMetrics) -> None:
        """Add another entry for the same model into this one."""
        _add_counts(self, other)
        if other.provider_metrics is not None:
            if self.provider_metrics is None:
                self.provider_metrics = {}
            _merge_numeric(self.provider_metrics, other.provider_metrics)


def _detailed_dict(record: RunMetrics) -> dict[str, Any]:
    """Return the stored dict of a usage record with a per-model breakdown, its ``details`` entries flattened too."""
    values = _flattened(record)
    if record.details is not None:
        # TODO(simplify): details entries keep an empty ``provider_metrics`` that ``ModelMetrics.to_dict`` drops.
        # Write each entry with ``to_dict``. Stored data would notice: details entries lose ``"provider_metrics": {}``.
        values["details"] = {
            model_type: [_compact(_flattened(entry), keep_empty=True) for entry in entries]
            for model_type, entries in record.details.items()
        }
    return _compact(values)


def _details_from_dict(details: Mapping[str, list[Any]]) -> dict[str, list[ModelMetrics]]:
    return {
        model_type: [ModelMetrics.from_dict(entry) if isinstance(entry, dict) else entry for entry in entries]
        for model_type, entries in details.items()
    }


@dataclass(slots=True, kw_only=True)
class RunMetrics(_Counts):
    """Usage one run accumulated over all its requests, with a per-model breakdown in ``details``.

    ``duration`` covers the same work as the counters: every leg of a run that paused and resumed. A team turn's
    usage keeps its leader's duration, since its members run inside the leader's run (``teams.runtime.team_usage``).
    """

    time_to_first_token: float | None = None
    duration: float | None = None
    details: dict[str, list[ModelMetrics]] | None = None

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
        return _detailed_dict(self)

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
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestUsage:
    """The usage one provider request reported: its time, its metrics, and the model that answered it.

    ``created_at`` is when the request was answered, except voice transcription, which dates it when it was sent.
    ``model`` is None when the usage report names no model.
    """

    created_at: float
    metrics: MessageMetrics
    model: ModelProvenance | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class VoiceDuration:
    """The billed duration of a live voice session so far, and whether the session has ended."""

    seconds: float
    finalized: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class HelperUsage:
    """The usage of paid model work that no stored run holds, such as one helper prompt's, without any of its content.

    ``run_id`` is the id the usage is recorded under. ``model`` and ``provider`` are the model and provider strings
    its usage row stores, None when unknown: a helper prompt stores its route's ``ModelIdentity.id`` and
    ``ModelIdentity.provider``, while other writers store a label of their own, such as the name Mem0's usage is
    priced by (``"OpenAI Chat"``). ``metrics`` sums its requests by model, and ``requests`` lists each request whose
    usage describes it alone: a retried request's usage sums its attempts, so ``metrics`` counts it but ``requests``
    does not list it. ``voice`` is set for a live voice session, which is billed by duration and lists no requests.
    ``created_at`` defaults to when the value is built, which is when a helper prompt finished; Mem0 dates it by its
    first call, voice transcription by when its request was sent, a helper run by its start, a live voice or realtime
    call by its start, and a compaction summary by when it was stored.
    """

    run_id: str
    model: str | None
    provider: str | None
    metrics: RunMetrics
    requests: tuple[RequestUsage, ...] = ()
    voice: VoiceDuration | None = None
    created_at: float = field(default_factory=lambda: int(time()))


def _count_model(totals: list[ModelMetrics], entry: ModelMetrics) -> None:
    """Add ``entry`` to the total of its ``(provider, id)``, or start one from a copy."""
    for total in totals:
        if (total.provider, total.id) == (entry.provider, entry.id):
            total.accumulate(entry)
            return
    # The copy drops an empty ``provider_metrics``.
    totals.append(ModelMetrics.from_dict(entry.to_dict()))


__all__ = [
    "TOKEN_FIELDS",
    "HelperUsage",
    "MessageMetrics",
    "ModelMetrics",
    "RequestUsage",
    "RunMetrics",
    "TokenCounts",
    "VoiceDuration",
    "add_usage",
]
