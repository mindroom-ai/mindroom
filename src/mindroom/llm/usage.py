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
    from collections.abc import Mapping

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


def _compact(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop None, zero integers, and empty dicts or lists."""
    return {
        key: value
        for key, value in values.items()
        if value is not None
        and (not isinstance(value, int) or value != 0)
        and (not isinstance(value, (dict, list)) or len(value) > 0)
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
    # LEGACY_COMPAT: Usage dicts stored with provider extras and a provider-reported cost.
    # Legacy format: request metrics on assistant messages and the per-model entries of run metrics hold a
    #   ``provider_metrics`` dict (Claude's ``server_tool_use``, ``service_tier``, and ``context_usage``, Gemini's
    #   ``traffic_type``, Ollama's timings, and through v2026.10.663 Groq's and Cerebras's timings), and request, run,
    #   and per-model metrics hold a ``cost`` when an OpenAI-compatible server reported ``usage.cost``; no MindRoom code
    #   read them but ``context_usage``.
    # Last legacy release: v2026.10.729; replacement: v2026.10.730, whose usage records store neither.
    # Handling: the keys are ignored on read and never written again, so a rewritten row drops them; Claude's
    #   ``context_usage`` reads as the request's ``context`` (``MessageMetrics.from_dict``).
    # Coverage: tests/llm/test_codecs.py::test_usage_stored_with_provider_extras_and_a_cost_reads_without_them,
    #   tests/llm/test_codecs.py::test_run_metrics_round_trip_agno_dict_minus_dropped_keys,
    #   tests/llm/test_codecs.py::test_model_metrics_round_trip_agno_dict_minus_dropped_keys
    return {**_known_fields(cls, data), "tokens": TokenCounts(**_known_fields(TokenCounts, data))}


@dataclass(slots=True, kw_only=True)
class _Counts:
    """The token counters every usage record carries, first in its stored dict."""

    tokens: TokenCounts = TokenCounts()

    @property
    def reported(self) -> bool:
        """Whether the provider reported usage: it did unless every counter is zero, whatever timings were taken."""
        return any(getattr(self.tokens, name) for name in TOKEN_FIELDS)

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict shape."""
        return _compact(_flattened(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        return cls(**_known_counts(cls, data))


def _sum_optional(left: float | None, right: float | None) -> float | None:
    """Add two durations a usage report may leave unset; None when neither is set."""
    if left is None:
        return right
    if right is None:
        return left
    return left + right


@dataclass(slots=True, kw_only=True)
class MessageMetrics(_Counts):
    """Usage one provider request reported; stored on the assistant message.

    ``duration`` is the finishing attempt's: a retried request's counters also hold the tokens its failed attempts
    reported, but its duration times only the attempt that finished.

    ``context`` holds the input, cache read, and cache write counters of the context the request held, each one
    reported, a zero one too, for a route whose billed counters can describe more than that context. Claude reports it
    on every response: a response native compaction ran in bills every iteration, while its final iteration alone
    describes the active context. A request whose usage sums retried attempts keeps the answering attempt's.
    """

    duration: float | None = None
    time_to_first_token: float | None = None
    context: TokenCounts | None = None

    def __add__(self, other: MessageMetrics) -> MessageMetrics:
        """Sum counters and duration; keep the first time to first token and the later report's context."""
        return MessageMetrics(
            tokens=self.tokens + other.tokens,
            duration=_sum_optional(self.duration, other.duration),
            time_to_first_token=(
                self.time_to_first_token if self.time_to_first_token is not None else other.time_to_first_token
            ),
            context=other.context,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict shape; ``context`` omits its zero counters too, since it reports every one."""
        values = _flattened(self)
        if self.context is not None:
            values["context"] = _compact(self.context.to_dict(first=_STORED_COUNTER_ORDER))
        return _compact(values)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        context = data.get("context")
        if context is None:
            # LEGACY_COMPAT: Request usage stored with Claude's context counters under ``provider_metrics``.
            # Legacy format: ``provider_metrics.context_usage`` (``input_tokens``, ``cache_read_tokens``, and
            #   ``cache_write_tokens``) in the metrics of an assistant message a Claude route answered.
            # Last legacy release: v2026.10.729; replacement: v2026.10.730, which stores them as ``context``.
            # Handling: read as the request's ``context``, so history sizing and the context a reply reports read an
            #   old Claude response as a new one.
            # Coverage: tests/llm/test_codecs.py::test_claude_context_usage_stored_under_provider_metrics_reads_as_context,
            #   tests/test_model_usage.py::test_native_final_iteration_context_wins_over_billed_counters,
            #   tests/llm/test_codecs.py::test_message_metrics_round_trip_agno_dict_minus_dropped_keys
            provider_metrics = data.get("provider_metrics")
            context = provider_metrics.get("context_usage") if isinstance(provider_metrics, dict) else None
        values = _known_counts(cls, data)
        values["context"] = TokenCounts(**_known_fields(TokenCounts, context)) if isinstance(context, dict) else None
        return cls(**values)


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

    def accumulate(self, other: ModelMetrics) -> None:
        """Add another entry for the same model into this one."""
        self.tokens = self.tokens + other.tokens


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
        """Count one request's usage under ``details["model"]`` for ``(provider, model_id)`` and in the totals."""
        entry = ModelMetrics(tokens=usage.tokens, id=model_id, provider=provider)
        if self.details is None:
            self.details = {}
        entries = self.details.setdefault("model", [])
        for existing in entries:
            if existing.id == model_id and existing.provider == provider:
                existing.accumulate(entry)
                break
        else:
            entries.append(entry)
        self.tokens = self.tokens + usage.tokens

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict shape, its ``details`` entries flattened too."""
        values = _flattened(self)
        if self.details is not None:
            values["details"] = {
                model_type: [entry.to_dict() for entry in entries] for model_type, entries in self.details.items()
            }
        return _compact(values)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        values = _known_counts(cls, data)
        if values.get("details") is not None:
            values["details"] = {
                model_type: [ModelMetrics.from_dict(entry) if isinstance(entry, dict) else entry for entry in entries]
                for model_type, entries in values["details"].items()
            }
        return cls(**values)

    def __add__(self, other: RunMetrics) -> RunMetrics:
        """Sum two runs' usage, merging ``details`` by model type, provider, and id."""
        result = RunMetrics(tokens=self.tokens + other.tokens, duration=_sum_optional(self.duration, other.duration))
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
    """Add ``entry`` to the total of its ``(provider, id)``, or start one from a copy, so ``entry`` stays as it was."""
    for total in totals:
        if (total.provider, total.id) == (entry.provider, entry.id):
            total.accumulate(entry)
            return
    totals.append(replace(entry))


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
