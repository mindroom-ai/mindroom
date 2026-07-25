"""Deterministic configuration, telemetry, and evidence for live Matrix stress runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from mindroom.synthetic_model import SyntheticPlan, synthetic_plan
from mindroom.tool_system.events import format_tool_combined

STRESS_TRACE_VERSION = 1
STRESS_BASELINE_VERSION = 1
DEFAULT_STRESS_ARTIFACT_ROOT = Path("artifacts/live-matrix-stress")
DEFAULT_PERFORMANCE_ALLOWANCE = 0.25
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:access[_-]?token|authorization|cookie|database[_-]?url|password|secret|sync[_-]?token)",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ACCESS_TOKEN_PATTERN = re.compile(r"\b(?:syt|MDA|sk)-[A-Za-z0-9._~-]{6,}\b")
_POSTGRES_URL_PATTERN = re.compile(r"(?i)\bpostgres(?:ql)?://[^\s\"']+")
_URL_PATTERN = re.compile(r"\bhttps?://[^\s\"']+")
_ROOM_ID_PATTERN = re.compile(r"![A-Za-z0-9._~=/+-]+:[A-Za-z0-9._:-]+")
_EVENT_ID_PATTERN = re.compile(r"\$[A-Za-z0-9._~=/+-]+")
_USER_ID_PATTERN = re.compile(r"@[A-Za-z0-9._=/-]+:[A-Za-z0-9._:-]+")
_SYNC_TOKEN_PATTERN = re.compile(r"\bs\d+_[A-Za-z0-9._~=/+-]{8,}\b")
_PRIVATE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9.])/(?:Users|home|private|tmp)/[^\s\"']+")
_POSTGRES_CONCURRENCY_ERRORS = (
    "another command is already in progress",
    "another operation is in progress",
)
_INTERRUPTION_SIGNALS = (
    "response interrupted",
    "response was interrupted",
    "restart interrupted",
)
_UNCAUGHT_SIGNALS = (
    "uncaught exception",
    "unhandled exception",
    "task exception was never retrieved",
    "traceback (most recent call last)",
)
_EVENT_LOOP_PROBE_CEILING_MS = 5000.0
_SYNC_STARVATION_CEILING_SECONDS = 120.0


def _positive(value: float, field_name: str) -> None:
    if value <= 0:
        msg = f"{field_name} must be positive"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class StressConfig:
    """Replayable synthetic workload shape for one stress run."""

    threads: int = 100
    rooms: int = 10
    stream_seconds: float = 45.0
    edit_interval: float = 0.5
    chars_per_second: float = 80.0
    tool_call_probability: float = 1.0
    min_sleep_seconds: int = 1
    max_sleep_seconds: int = 3
    waves: int = 2
    history_turns: int = 20
    cache_backend: Literal["postgres"] = "postgres"
    seed: int = 1
    overlapping_followups: bool = False
    fault_mode: Literal["none", "serialize-streams"] = "none"
    barrier_timeout_seconds: float = 90.0
    settlement_timeout_seconds: float = 180.0
    resource_sample_interval_seconds: float = 0.5

    def validate(self) -> None:
        """Reject shapes that cannot provide exact deterministic evidence."""
        _positive(self.threads, "threads")
        _positive(self.rooms, "rooms")
        if self.rooms > self.threads:
            msg = "rooms cannot exceed threads"
            raise ValueError(msg)
        _positive(self.stream_seconds, "stream_seconds")
        _positive(self.edit_interval, "edit_interval")
        _positive(self.chars_per_second, "chars_per_second")
        if not 0 <= self.tool_call_probability <= 1:
            msg = "tool_call_probability must be between 0 and 1"
            raise ValueError(msg)
        if self.min_sleep_seconds < 0:
            msg = "min_sleep_seconds must be non-negative"
            raise ValueError(msg)
        if self.max_sleep_seconds < self.min_sleep_seconds:
            msg = "max_sleep_seconds must be greater than or equal to min_sleep_seconds"
            raise ValueError(msg)
        _positive(self.waves, "waves")
        if self.history_turns < 0:
            msg = "history_turns must be non-negative"
            raise ValueError(msg)
        if self.cache_backend != "postgres":
            msg = "stress profile requires PostgreSQL and never falls back to SQLite"
            raise ValueError(msg)
        if self.fault_mode not in {"none", "serialize-streams"}:
            msg = f"unsupported stress fault mode: {self.fault_mode}"
            raise ValueError(msg)
        _positive(self.barrier_timeout_seconds, "barrier_timeout_seconds")
        _positive(self.settlement_timeout_seconds, "settlement_timeout_seconds")
        _positive(self.resource_sample_interval_seconds, "resource_sample_interval_seconds")
        exact_pulses = self.stream_seconds / self.edit_interval
        if not math.isclose(exact_pulses, round(exact_pulses), abs_tol=1e-9):
            msg = "stream_seconds must be exactly divisible by edit_interval"
            raise ValueError(msg)

    @property
    def min_response_chars(self) -> int:
        """Return the shortest seeded response at half the configured duration."""
        return max(64, round(self.stream_seconds * self.chars_per_second / 2))

    @property
    def max_response_chars(self) -> int:
        """Return the longest seeded response at the configured duration."""
        return max(self.min_response_chars, round(self.stream_seconds * self.chars_per_second))

    @property
    def chunk_chars(self) -> int:
        """Return the chunk size matching the configured update cadence."""
        return max(1, round(self.edit_interval * self.chars_per_second))

    def marker(self, wave: int, thread: int) -> str:
        """Build one deterministic synthetic request marker."""
        if wave not in range(self.waves) or thread not in range(self.threads):
            msg = f"stress marker outside configured shape: wave={wave}, thread={thread}"
            raise ValueError(msg)
        return f"LMS[wave={wave};thread={thread:03d};seed={self.seed}]"

    def to_json(self) -> str:
        """Serialize one replayable stress scenario."""
        self.validate()
        return json.dumps(
            {"version": STRESS_TRACE_VERSION, "profile": "stress", "config": asdict(self)},
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> StressConfig:
        """Load and validate one stress scenario."""
        payload = json.loads(value)
        if (
            not isinstance(payload, dict)
            or payload.get("version") != STRESS_TRACE_VERSION
            or payload.get("profile") != "stress"
        ):
            msg = "unsupported live Matrix stress trace"
            raise ValueError(msg)
        raw_config = payload.get("config")
        if not isinstance(raw_config, dict):
            msg = "live Matrix stress trace is missing config"
            raise TypeError(msg)
        config = cls(**cast("dict[str, Any]", raw_config))
        config.validate()
        return config


@dataclass(frozen=True, slots=True)
class StressRequest:
    """Logical identity parsed from one synthetic model request."""

    wave: int
    thread: int
    seed: int

    @property
    def label(self) -> str:
        """Return a stable artifact-safe logical label."""
        return f"wave-{self.wave:02d}/thread-{self.thread:03d}"


class SyntheticStressAudit:
    """Audit exact built-in synthetic-model behavior from append-only telemetry."""

    def __init__(self, config: StressConfig, telemetry_path: Path) -> None:
        config.validate()
        self.config = config
        self.telemetry_path = telemetry_path

    def plan(self, request: StressRequest) -> SyntheticPlan:
        """Return the exact seeded model plan for one stress request."""
        self._validate_request(request)
        identity = self.config.marker(request.wave, request.thread)
        return synthetic_plan(
            identity,
            seed=self.config.seed,
            min_response_chars=self.config.min_response_chars,
            max_response_chars=self.config.max_response_chars,
            tool_call_probability=self.config.tool_call_probability,
            min_sleep_seconds=self.config.min_sleep_seconds,
            max_sleep_seconds=self.config.max_sleep_seconds,
            tool_available=True,
        )

    def expected_body(self, request: StressRequest) -> str:
        """Return the exact model-generated body for one configured request."""
        return self.plan(request).body

    def expected_matrix_body(self, request: StressRequest) -> str:
        """Return the exact body after MindRoom inserts its visible tool marker."""
        plan = self.plan(request)
        if plan.split_at is None or plan.sleep_seconds is None:
            return plan.body
        tool_marker, _ = format_tool_combined(
            "sleep",
            {"seconds": plan.sleep_seconds},
            None,
            tool_index=1,
        )
        return plan.prefix + tool_marker + plan.suffix

    def reached_count(self, wave: int) -> int:
        """Return distinct initial requests recorded at one wave barrier."""
        return len(
            {
                str(event["request_id"])
                for event in self._events()
                if event.get("kind") == "barrier_reached"
                and event.get("phase") == "initial"
                and event.get("group") == str(wave)
            },
        )

    def snapshot(self) -> dict[str, object]:
        """Return deterministic request counts, concurrency, and response shape."""
        events = self._stress_events()
        counts = Counter((str(event.get("kind")), str(event.get("phase"))) for event in events)
        plans = [
            self.plan(StressRequest(wave=wave, thread=thread, seed=self.config.seed))
            for wave in range(self.config.waves)
            for thread in range(self.config.threads)
        ]
        timeline, max_active_streams = self._active_timeline(events)
        return {
            "reached_by_wave": {str(wave): self.reached_count(wave) for wave in range(self.config.waves)},
            "event_counts": {f"{kind}:{phase}": count for (kind, phase), count in sorted(counts.items())},
            "max_active_streams": max_active_streams,
            "active_stream_timeline": timeline,
            "response_chars": latency_summary([float(len(plan.body)) for plan in plans]),
            "tool_calls": sum(plan.tool_call_id is not None for plan in plans),
            "sleep_seconds": latency_summary(
                [float(plan.sleep_seconds) for plan in plans if plan.sleep_seconds is not None],
            ),
        }

    def assert_complete(self) -> None:
        """Fail unless every seeded request completed every expected phase once."""
        events = self._stress_events()
        failures: list[str] = []
        for wave in range(self.config.waves):
            reached = self.reached_count(wave)
            if reached != self.config.threads:
                failures.append(f"wave {wave} barrier reached {reached}/{self.config.threads}")
        by_request = Counter(
            (
                str(event.get("request_id")),
                str(event.get("kind")),
                str(event.get("phase")),
            )
            for event in events
        )
        for wave in range(self.config.waves):
            for thread in range(self.config.threads):
                request = StressRequest(wave=wave, thread=thread, seed=self.config.seed)
                plan = self.plan(request)
                expected = {
                    ("barrier_reached", "initial"): 1,
                    ("request_started", "initial"): 1,
                    ("request_finished", "initial"): 1,
                    ("tool_call_emitted", "initial"): int(plan.tool_call_id is not None),
                    ("request_started", "continuation"): int(plan.tool_call_id is not None),
                    ("request_finished", "continuation"): int(plan.tool_call_id is not None),
                }
                for (kind, phase), count in expected.items():
                    actual = by_request[(plan.request_id, kind, phase)]
                    if actual != count:
                        failures.append(
                            f"{request.label} {kind}:{phase} count {actual}/{count}",
                        )
        max_active_streams = self._active_timeline(events)[1]
        if max_active_streams != self.config.threads:
            failures.append(
                f"maximum active model streams {max_active_streams}/{self.config.threads}",
            )
        if failures:
            raise AssertionError("; ".join(failures))

    def _validate_request(self, request: StressRequest) -> None:
        if request.seed != self.config.seed:
            msg = f"stress request seed {request.seed} does not match configured seed {self.config.seed}"
            raise ValueError(msg)
        if request.wave not in range(self.config.waves) or request.thread not in range(self.config.threads):
            msg = f"stress request outside configured shape: {request.label}"
            raise ValueError(msg)

    def _events(self) -> list[dict[str, object]]:
        if not self.telemetry_path.exists():
            return []
        events: list[dict[str, object]] = []
        for line in self.telemetry_path.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            if not isinstance(payload, dict):
                msg = "synthetic model telemetry entry must be an object"
                raise TypeError(msg)
            events.append(cast("dict[str, object]", payload))
        return events

    def _stress_events(self) -> list[dict[str, object]]:
        request_ids = {
            self.plan(
                StressRequest(
                    wave=wave,
                    thread=thread,
                    seed=self.config.seed,
                ),
            ).request_id
            for wave in range(self.config.waves)
            for thread in range(self.config.threads)
        }
        return [event for event in self._events() if event.get("request_id") in request_ids]

    @staticmethod
    def _active_timeline(events: Sequence[Mapping[str, object]]) -> tuple[list[dict[str, object]], int]:
        ordered = sorted(
            (
                (float(event["time"]), 1 if event.get("kind") == "request_started" else -1)
                for event in events
                if event.get("kind") in {"request_started", "request_finished"}
            ),
            key=lambda item: (item[0], -item[1]),
        )
        if not ordered:
            return [], 0
        origin = ordered[0][0]
        active = 0
        maximum = 0
        timeline: list[dict[str, object]] = []
        for timestamp, delta in ordered:
            active += delta
            maximum = max(maximum, active)
            timeline.append(
                {
                    "offset_seconds": round(timestamp - origin, 3),
                    "active_streams": active,
                },
            )
        return timeline, maximum


def percentile(values: Sequence[float], percentile_value: float) -> float:
    """Return a linearly interpolated percentile using sorted observations."""
    if not values:
        msg = "cannot calculate a percentile without observations"
        raise ValueError(msg)
    if not 0 <= percentile_value <= 100:
        msg = "percentile must be between 0 and 100"
        raise ValueError(msg)
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile_value / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def latency_summary(values: Sequence[float]) -> dict[str, float | int]:
    """Summarize one timing family with all stress-gate percentiles."""
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "p50": round(percentile(values, 50), 3),
        "p90": round(percentile(values, 90), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
        "max": round(max(values), 3),
    }


@dataclass(slots=True)
class StressLogMetrics:
    """Aggregate production structured logs into explicit stress signals."""

    room_barrier_count: int = 0
    thread_barrier_count: int = 0
    known_thread_room_barrier_count: int = 0
    predecessor_wait_ms: list[float] = field(default_factory=list)
    update_run_ms: list[float] = field(default_factory=list)
    update_total_ms: list[float] = field(default_factory=list)
    coalesced_intermediate_writes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    full_scans: int = 0
    snapshot_store_outcomes: Counter[str] = field(default_factory=Counter)
    cache_invalidated_reasons: Counter[str] = field(default_factory=Counter)
    missing_raw_cache_skips: int = 0
    live_append_successes: int = 0
    live_append_failures: int = 0
    repair_fetches_by_thread: Counter[str] = field(default_factory=Counter)
    reservation_created: int = 0
    reservation_alias_transitions: int = 0
    reservation_released: int = 0
    reservation_expired: int = 0
    reservation_active_final: int | None = None
    watchdog_stalls: int = 0
    sync_restarts: int = 0
    sync_restart_retries: int = 0
    interruptions: int = 0
    uncaught_exceptions: int = 0
    postgres_concurrency_errors: int = 0
    cache_store_failures: int = 0

    def ingest(self, event: Mapping[str, object]) -> None:
        """Fold one structured log event into metric counters."""
        message = str(event.get("event", event.get("message", "")))
        lowered = message.lower()
        self._ingest_outbound_schedule(message, event)
        self._ingest_update_timing(message, event)
        self._ingest_cache_history(message, lowered, event)
        self._ingest_reservation(message, event)
        self._ingest_health(lowered)
        if "cache" in lowered and ("failure" in lowered or "failed" in lowered) and event.get("outcome") == "success":
            self.cache_store_failures += 1

    def _ingest_outbound_schedule(
        self,
        message: str,
        event: Mapping[str, object],
    ) -> None:
        """Count outbound cache barrier routing decisions."""
        if message != "Event cache outbound schedule timing":
            return
        barrier_kind = event.get("barrier_kind")
        if message == "Event cache outbound schedule timing":
            if barrier_kind == "room":
                self.room_barrier_count += 1
                if event.get("is_edit") is True:
                    self.known_thread_room_barrier_count += 1
            elif barrier_kind == "thread":
                self.thread_barrier_count += 1

    def _ingest_update_timing(
        self,
        message: str,
        event: Mapping[str, object],
    ) -> None:
        """Collect cache write queue and execution timings."""
        if message != "Event cache update timing":
            return
        self._append_numeric(event, "predecessor_wait_ms", self.predecessor_wait_ms)
        self._append_numeric(event, "update_run_ms", self.update_run_ms)
        self._append_numeric(event, "total_ms", self.update_total_ms)
        self.coalesced_intermediate_writes += _integer(event.get("coalesced_update_count"))
        if event.get("outcome") == "error":
            self.cache_store_failures += 1

    def _ingest_cache_history(
        self,
        message: str,
        lowered: str,
        event: Mapping[str, object],
    ) -> None:
        """Collect cache read, repair, store, and append outcomes."""
        if message == "matrix_cache_thread_history_refreshed":
            mode = event.get("mode")
            if mode == "cache_hit":
                self.cache_hits += 1
            elif mode == "full_scan":
                self.cache_misses += 1
                self.full_scans += 1
                thread_label = str(event.get("thread_id", "<unknown-thread>"))
                self.repair_fetches_by_thread[thread_label] += 1
            reject_reason = event.get("cache_reject_reason")
            if isinstance(reject_reason, str) and reject_reason:
                self.cache_invalidated_reasons[reject_reason] += 1
        if message == "Thread history cache store completed":
            outcome = str(event.get("cache_store_outcome", event.get("outcome", "unknown")))
            self.snapshot_store_outcomes[outcome] += 1
            if outcome in {"writes_unavailable", "error", "failed"}:
                self.cache_store_failures += 1
        if "missing raw cache" in lowered or event.get("reason") == "missing_raw_cache":
            self.missing_raw_cache_skips += 1
        if "append thread event to cache" in lowered:
            if "failed" in lowered or event.get("outcome") == "error":
                self.live_append_failures += 1
                self.cache_store_failures += 1
            else:
                self.live_append_successes += 1

    def _ingest_reservation(
        self,
        message: str,
        event: Mapping[str, object],
    ) -> None:
        """Collect reservation lifecycle and terminal leak telemetry."""
        if message == "outbound_thread_reservation":
            action = event.get("action")
            if action == "created":
                self.reservation_created += 1
            elif action == "alias_transition":
                self.reservation_alias_transitions += 1
            elif action == "released":
                self.reservation_released += 1
            elif action == "expired":
                self.reservation_expired += 1
            active_count = event.get("active_count")
            if isinstance(active_count, int):
                self.reservation_active_final = active_count

    def _ingest_health(self, lowered: str) -> None:
        """Count runtime health, restart, and database failure signals."""
        if "event_loop_stall_detected" in lowered:
            self.watchdog_stalls += 1
        if "sync restart" in lowered or "sync_watchdog_restart" in lowered:
            self.sync_restarts += 1
        if "sync_restart_retry_started" in lowered:
            self.sync_restart_retries += 1
        if any(signal in lowered for signal in _INTERRUPTION_SIGNALS):
            self.interruptions += 1
        if any(signal in lowered for signal in _UNCAUGHT_SIGNALS):
            self.uncaught_exceptions += 1
        if any(signal in lowered for signal in _POSTGRES_CONCURRENCY_ERRORS):
            self.postgres_concurrency_errors += 1

    @staticmethod
    def _append_numeric(
        event: Mapping[str, object],
        key: str,
        destination: list[float],
    ) -> None:
        value = event.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            destination.append(float(value))

    def summary(self) -> dict[str, object]:
        """Return JSON-safe aggregate telemetry."""
        return {
            "barriers": {
                "room": self.room_barrier_count,
                "thread": self.thread_barrier_count,
                "known_thread_room_violations": self.known_thread_room_barrier_count,
            },
            "write_coordination": {
                "predecessor_wait_ms": latency_summary(self.predecessor_wait_ms),
                "update_run_ms": latency_summary(self.update_run_ms),
                "total_ms": latency_summary(self.update_total_ms),
                "coalesced_intermediate_writes": self.coalesced_intermediate_writes,
            },
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
                "full_scans": self.full_scans,
                "snapshot_store_outcomes": dict(self.snapshot_store_outcomes),
                "invalidated_reasons": dict(self.cache_invalidated_reasons),
                "missing_raw_cache_skips": self.missing_raw_cache_skips,
                "live_append_successes": self.live_append_successes,
                "live_append_failures": self.live_append_failures,
                "repair_fetches_by_thread": dict(self.repair_fetches_by_thread),
            },
            "reservations": {
                "created": self.reservation_created,
                "alias_transitions": self.reservation_alias_transitions,
                "released": self.reservation_released,
                "expired": self.reservation_expired,
                "active_final": self.reservation_active_final,
            },
            "health": {
                "watchdog_stalls": self.watchdog_stalls,
                "sync_restarts": self.sync_restarts,
                "sync_restart_retries": self.sync_restart_retries,
                "interruptions": self.interruptions,
                "uncaught_exceptions": self.uncaught_exceptions,
                "postgres_concurrency_errors": self.postgres_concurrency_errors,
                "cache_store_failures": self.cache_store_failures,
            },
        }

    def assert_healthy(self, *, check_duplicate_repairs: bool = True) -> None:
        """Fail on correctness signals that invalidate a normal stress run."""
        failures: list[str] = []
        if self.known_thread_room_barrier_count:
            failures.append(
                f"{self.known_thread_room_barrier_count} known-thread edits used room barriers",
            )
        if self.reservation_active_final not in {None, 0}:
            failures.append(f"{self.reservation_active_final} thread reservations leaked")
        health_counts = (
            (self.watchdog_stalls, "event-loop watchdog stalls"),
            (self.sync_restarts, "internal sync restarts"),
            (self.sync_restart_retries, "sync-restart retries"),
            (self.interruptions, "interruption signals"),
            (self.uncaught_exceptions, "uncaught exceptions"),
            (self.postgres_concurrency_errors, "PostgreSQL connection-concurrency errors"),
            (self.cache_store_failures, "cache/store failures"),
        )
        failures.extend(f"{count} {label}" for count, label in health_counts if count)
        if check_duplicate_repairs:
            duplicate_repairs = {thread: count for thread, count in self.repair_fetches_by_thread.items() if count > 1}
            if duplicate_repairs:
                failures.append(f"duplicate repair scans: {duplicate_repairs}")
        if failures:
            raise AssertionError("; ".join(failures))


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_structured_log(text: str) -> tuple[list[dict[str, object]], list[str]]:
    """Parse JSON log lines while retaining non-JSON lines for signal scans."""
    events: list[dict[str, object]] = []
    raw_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            raw_lines.append(stripped)
            continue
        if isinstance(payload, dict):
            events.append(cast("dict[str, object]", payload))
        else:
            raw_lines.append(stripped)
    return events, raw_lines


def aggregate_log_metrics(text: str) -> StressLogMetrics:
    """Aggregate JSON and fallback text logs without dropping fatal signals."""
    events, raw_lines = parse_structured_log(text)
    metrics = StressLogMetrics()
    for event in events:
        metrics.ingest(event)
    for line in raw_lines:
        metrics.ingest({"event": line})
    return metrics


@dataclass(frozen=True, slots=True)
class BaselineSample:
    """One exact-run performance observation."""

    source_to_final_p95_ms: float
    source_to_final_p99_ms: float
    throughput_responses_per_second: float


@dataclass(frozen=True, slots=True)
class StressBaseline:
    """Versioned median-of-three-or-more baseline for one named profile."""

    profile: str
    source_revision: str
    config_sha256: str
    machine_class: str
    samples: tuple[BaselineSample, ...]
    allowance: float = DEFAULT_PERFORMANCE_ALLOWANCE

    def validate(self) -> None:
        """Reject incomplete, high-variance, or arbitrary baselines."""
        if not self.profile or not self.source_revision or not self.config_sha256 or not self.machine_class:
            msg = "baseline identity fields must be non-empty"
            raise ValueError(msg)
        if len(self.samples) < 3:
            msg = "performance baselines require at least three clean executions"
            raise ValueError(msg)
        if not 0 < self.allowance <= 0.5:
            msg = "baseline allowance must be positive and at most 50%"
            raise ValueError(msg)
        for sample in self.samples:
            _positive(sample.source_to_final_p95_ms, "source_to_final_p95_ms")
            _positive(sample.source_to_final_p99_ms, "source_to_final_p99_ms")
            _positive(sample.throughput_responses_per_second, "throughput_responses_per_second")
        for metric in (
            "source_to_final_p95_ms",
            "source_to_final_p99_ms",
            "throughput_responses_per_second",
        ):
            values = [float(getattr(sample, metric)) for sample in self.samples]
            median = statistics.median(values)
            dispersion = (max(values) - min(values)) / median
            if dispersion > self.allowance:
                msg = (
                    f"baseline {metric} dispersion {dispersion:.3f} exceeds "
                    f"allowance {self.allowance:.3f}; fix determinism before gating"
                )
                raise ValueError(msg)

    def medians(self) -> dict[str, float]:
        """Return metric medians used by regression gates."""
        self.validate()
        return {
            field_name: statistics.median(float(getattr(sample, field_name)) for sample in self.samples)
            for field_name in (
                "source_to_final_p95_ms",
                "source_to_final_p99_ms",
                "throughput_responses_per_second",
            )
        }

    def compare(
        self,
        candidate: BaselineSample,
        *,
        machine_class: str | None = None,
        enforce: bool = True,
    ) -> dict[str, object]:
        """Return comparison details and optionally enforce the allowance."""
        candidate_machine_class = machine_class or current_machine_class()
        if candidate_machine_class != self.machine_class:
            msg = (
                "stress baseline machine class mismatch: "
                f"baseline={self.machine_class!r}, candidate={candidate_machine_class!r}"
            )
            raise ValueError(msg)
        medians = self.medians()
        limits = {
            "source_to_final_p95_ms": medians["source_to_final_p95_ms"] * (1 + self.allowance),
            "source_to_final_p99_ms": medians["source_to_final_p99_ms"] * (1 + self.allowance),
            "throughput_responses_per_second": medians["throughput_responses_per_second"] * (1 - self.allowance),
        }
        failures: list[str] = []
        if candidate.source_to_final_p95_ms > limits["source_to_final_p95_ms"]:
            failures.append("source_to_final_p95_ms")
        if candidate.source_to_final_p99_ms > limits["source_to_final_p99_ms"]:
            failures.append("source_to_final_p99_ms")
        if candidate.throughput_responses_per_second < limits["throughput_responses_per_second"]:
            failures.append("throughput_responses_per_second")
        result = {
            "baseline_medians": {key: round(value, 3) for key, value in medians.items()},
            "limits": {key: round(value, 3) for key, value in limits.items()},
            "candidate": asdict(candidate),
            "allowance": self.allowance,
            "machine_class": self.machine_class,
            "passed": not failures,
            "regressed_metrics": failures,
        }
        if failures and enforce:
            msg = f"stress performance regression: {', '.join(failures)}"
            raise AssertionError(msg)
        return result

    def to_json(self) -> str:
        """Serialize one sanitized versioned baseline."""
        self.validate()
        return json.dumps(
            {
                "version": STRESS_BASELINE_VERSION,
                "profile": self.profile,
                "source_revision": self.source_revision,
                "config_sha256": self.config_sha256,
                "machine_class": self.machine_class,
                "allowance": self.allowance,
                "samples": [asdict(sample) for sample in self.samples],
                "medians": self.medians(),
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> StressBaseline:
        """Parse a complete versioned baseline."""
        payload = json.loads(value)
        if not isinstance(payload, dict) or payload.get("version") != STRESS_BASELINE_VERSION:
            msg = "unsupported or missing live Matrix stress baseline version"
            raise ValueError(msg)
        raw_samples = payload.get("samples")
        if not isinstance(raw_samples, list):
            msg = "live Matrix stress baseline is missing samples"
            raise TypeError(msg)
        baseline = cls(
            profile=str(payload.get("profile", "")),
            source_revision=str(payload.get("source_revision", "")),
            config_sha256=str(payload.get("config_sha256", "")),
            machine_class=str(payload.get("machine_class", "")),
            allowance=float(payload.get("allowance", DEFAULT_PERFORMANCE_ALLOWANCE)),
            samples=tuple(
                BaselineSample(**cast("dict[str, float]", sample)) for sample in raw_samples if isinstance(sample, dict)
            ),
        )
        if len(baseline.samples) != len(raw_samples):
            msg = "live Matrix stress baseline contains a malformed sample"
            raise TypeError(msg)
        baseline.validate()
        return baseline


def current_machine_class() -> str:
    """Return a sanitized stable machine-class key for baseline comparability."""
    return f"{platform.system().lower()}-{platform.machine().lower()}-{os.cpu_count() or 0}cpu"


class ArtifactSanitizer:
    """Stable logical-ID and secret sanitizer for persistent stress evidence."""

    def __init__(self) -> None:
        self._labels: dict[str, dict[str, str]] = defaultdict(dict)

    def _label(self, family: str, value: str) -> str:
        labels = self._labels[family]
        if value not in labels:
            labels[value] = f"<{family}-{len(labels) + 1:03d}>"
        return labels[value]

    def text(self, value: str) -> str:
        """Redact credentials, URLs, paths, and raw Matrix identifiers."""
        sanitized = _BEARER_PATTERN.sub("<authorization>", value)
        sanitized = _ACCESS_TOKEN_PATTERN.sub("<secret>", sanitized)
        sanitized = _POSTGRES_URL_PATTERN.sub("<database-url>", sanitized)
        sanitized = _URL_PATTERN.sub("<url>", sanitized)
        sanitized = _SYNC_TOKEN_PATTERN.sub("<sync-token>", sanitized)
        sanitized = _PRIVATE_PATH_PATTERN.sub("<path>", sanitized)
        sanitized = _ROOM_ID_PATTERN.sub(lambda match: self._label("room", match.group()), sanitized)
        sanitized = _EVENT_ID_PATTERN.sub(lambda match: self._label("event", match.group()), sanitized)
        return _USER_ID_PATTERN.sub(lambda match: self._label("user", match.group()), sanitized)

    def value(self, value: object, *, key: str | None = None) -> object:
        """Recursively sanitize one JSON-safe value."""
        if key is not None and _SENSITIVE_KEY_PATTERN.search(key):
            return "<redacted>"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {
                self.text(str(item_key)): self.value(item_value, key=str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        return value

    def assert_clean(self, value: str) -> None:
        """Reject sanitizer output that still contains a forbidden secret class."""
        forbidden = {
            "authorization": _BEARER_PATTERN,
            "access token": _ACCESS_TOKEN_PATTERN,
            "database URL": _POSTGRES_URL_PATTERN,
            "private path": _PRIVATE_PATH_PATTERN,
            "room ID": _ROOM_ID_PATTERN,
            "event ID": _EVENT_ID_PATTERN,
            "user ID": _USER_ID_PATTERN,
            "sync token": _SYNC_TOKEN_PATTERN,
        }
        leaked = [name for name, pattern in forbidden.items() if pattern.search(value)]
        if leaked:
            msg = f"stress artifact sanitizer leaked: {', '.join(leaked)}"
            raise AssertionError(msg)


def _is_forbidden_artifact_root(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    forbidden_roots = (Path("/tmp"), Path("/private/tmp"), Path("/var/tmp"))  # noqa: S108
    if any(resolved == root or resolved.is_relative_to(root) for root in forbidden_roots):
        return True
    return "tmp" in {part.lower() for part in resolved.parts}


class StressArtifactBundle:
    """Persistent atomic and sanitized evidence for successful or failed stress runs."""

    def __init__(self, directory: Path, *, sanitizer: ArtifactSanitizer | None = None) -> None:
        self.directory = directory
        self.sanitizer = sanitizer or ArtifactSanitizer()

    @classmethod
    def create(cls, root: Path, run_id: str) -> StressArtifactBundle:
        """Create one persistent non-temporary run directory."""
        if _is_forbidden_artifact_root(root):
            msg = f"stress artifact root must be persistent and not temporary: {root}"
            raise ValueError(msg)
        if not run_id or "/" in run_id:
            msg = "stress run ID must be one path component"
            raise ValueError(msg)
        directory = root.expanduser().resolve() / run_id
        directory.mkdir(parents=True, exist_ok=False)
        return cls(directory)

    def write_json(self, name: str, payload: object) -> Path:
        """Atomically write one recursively sanitized JSON artifact."""
        sanitized = self.sanitizer.value(payload)
        serialized = json.dumps(sanitized, indent=2, sort_keys=True) + "\n"
        self.sanitizer.assert_clean(serialized)
        return self._atomic_write(name, serialized)

    def write_text(self, name: str, payload: str) -> Path:
        """Atomically write one sanitized text artifact."""
        sanitized = self.sanitizer.text(payload)
        self.sanitizer.assert_clean(sanitized)
        return self._atomic_write(name, sanitized)

    def _atomic_write(self, name: str, payload: str) -> Path:
        if Path(name).name != name:
            msg = f"stress artifact name must be one path component: {name}"
            raise ValueError(msg)
        destination = self.directory / name
        temporary = destination.with_suffix(destination.suffix + ".pending")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(destination)
        return destination


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """Bounded process and dependency health sample."""

    offset_seconds: float
    cpu_percent: float
    rss_bytes: int
    sync_age_seconds: float | None
    health_latency_ms: float | None
    tuwunel_healthy: bool
    postgres_healthy: bool


def resource_summary(samples: Sequence[ResourceSample]) -> dict[str, object]:
    """Summarize bounded runtime resource and health samples."""
    if not samples:
        return {"count": 0}
    cpu = [sample.cpu_percent for sample in samples]
    rss = [float(sample.rss_bytes) for sample in samples]
    sync_age = [sample.sync_age_seconds for sample in samples if sample.sync_age_seconds is not None]
    health_latency = [sample.health_latency_ms for sample in samples if sample.health_latency_ms is not None]
    return {
        "count": len(samples),
        "cpu_percent": latency_summary(cpu),
        "rss_bytes": latency_summary(rss),
        "sync_age_seconds": latency_summary(sync_age),
        "health_latency_ms": latency_summary(health_latency),
        "event_loop_probe_latency_ms": latency_summary(health_latency),
        "mindroom_health_probe_failures": sum(sample.health_latency_ms is None for sample in samples),
        "tuwunel_unhealthy_samples": sum(not sample.tuwunel_healthy for sample in samples),
        "postgres_unhealthy_samples": sum(not sample.postgres_healthy for sample in samples),
    }


def assert_resource_health(summary: Mapping[str, object]) -> None:
    """Fail on dependency loss, event-loop starvation, or stale Matrix sync."""
    dependency_failures = (
        (_integer(summary.get("mindroom_health_probe_failures")), "MindRoom health probe failures"),
        (_integer(summary.get("tuwunel_unhealthy_samples")), "Tuwunel unhealthy samples"),
        (_integer(summary.get("postgres_unhealthy_samples")), "PostgreSQL unhealthy samples"),
    )
    failures = [f"{count} {label}" for count, label in dependency_failures if count]
    event_loop_probe = summary.get("event_loop_probe_latency_ms")
    if isinstance(event_loop_probe, Mapping):
        maximum = event_loop_probe.get("max")
        if isinstance(maximum, int | float) and maximum > _EVENT_LOOP_PROBE_CEILING_MS:
            failures.append(
                f"event-loop progress probe exceeded {_EVENT_LOOP_PROBE_CEILING_MS:g} ms: {maximum:g} ms",
            )
    sync_age = summary.get("sync_age_seconds")
    if isinstance(sync_age, Mapping):
        maximum = sync_age.get("max")
        if isinstance(maximum, int | float) and maximum > _SYNC_STARVATION_CEILING_SECONDS:
            failures.append(
                f"Matrix sync age exceeded {_SYNC_STARVATION_CEILING_SECONDS:g} s watchdog boundary: {maximum:g} s",
            )
    if failures:
        raise AssertionError("; ".join(failures))


@dataclass(slots=True)
class ManagedStressPostgres:
    """Exact disposable PostgreSQL container owned by one stress stack."""

    name: str
    password: str = "synthetic-stress-password"  # noqa: S105 - disposable synthetic database
    image: str = "postgres:15-alpine"
    host_port: int | None = None
    _started: bool = False

    @property
    def started(self) -> bool:
        """Return whether the owned PostgreSQL container should still exist."""
        return self._started

    @property
    def database_url(self) -> str:
        """Return the local synthetic DSN used only by the live child."""
        if self.host_port is None:
            msg = "stress PostgreSQL has not started"
            raise RuntimeError(msg)
        return f"postgresql://cache:{self.password}@127.0.0.1:{self.host_port}/mindroom"

    def start(self) -> None:
        """Start and verify a disposable PostgreSQL container."""
        subprocess.run(
            (
                "docker",
                "run",
                "--detach",
                "--name",
                self.name,
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_USER=cache",
                "--env",
                f"POSTGRES_PASSWORD={self.password}",
                "--env",
                "POSTGRES_DB=mindroom",
                self.image,
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self._started = True
        port_result = subprocess.run(
            ("docker", "port", self.name, "5432/tcp"),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        endpoint = port_result.stdout.strip().rsplit(":", maxsplit=1)
        if len(endpoint) != 2 or not endpoint[1].isdigit():
            msg = f"could not resolve stress PostgreSQL host port: {port_result.stdout!r}"
            raise RuntimeError(msg)
        self.host_port = int(endpoint[1])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ("docker", "exec", self.name, "pg_isready", "-U", "cache", "-d", "mindroom"),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0:
                return
            time.sleep(0.2)
        msg = "stress PostgreSQL did not become ready within 60 seconds"
        raise TimeoutError(msg)

    def is_healthy(self) -> bool:
        """Return whether the exact container is running and PostgreSQL responds."""
        if not self._started:
            return False
        result = subprocess.run(
            ("docker", "exec", self.name, "pg_isready", "-U", "cache", "-d", "mindroom"),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0

    def diagnostics(self) -> dict[str, object]:
        """Capture sanitized connection and table activity diagnostics."""
        if not self._started:
            return {"started": False}
        query = (
            "SELECT datname, state, count(*) "
            "FROM pg_stat_activity WHERE datname = 'mindroom' GROUP BY datname, state ORDER BY state;"
        )
        result = subprocess.run(
            ("docker", "exec", self.name, "psql", "-U", "cache", "-d", "mindroom", "-Atc", query),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "started": True,
            "healthy": self.is_healthy(),
            "image": self.image,
            "activity": result.stdout.splitlines(),
            "diagnostic_exit_code": result.returncode,
        }

    def clear_cache_namespace(self, namespace: str) -> None:
        """Delete cache data rows while retaining the runtime certification generation."""
        if not self._started:
            msg = "cannot clear a stress PostgreSQL namespace before startup"
            raise RuntimeError(msg)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", namespace):
            msg = f"invalid synthetic cache namespace: {namespace!r}"
            raise ValueError(msg)
        tables = (
            "mindroom_event_cache_thread_events",
            "mindroom_event_cache_events",
            "mindroom_event_cache_event_edits",
            "mindroom_event_cache_event_threads",
            "mindroom_event_cache_redacted_events",
            "mindroom_event_cache_mxc_text",
            "mindroom_event_cache_event_mxc_references",
            "mindroom_event_cache_thread_state",
            "mindroom_event_cache_room_state",
        )
        statements = " ".join(
            (
                f"DELETE FROM {table} WHERE namespace = '{namespace}' "  # noqa: S608 - table allowlist and validated namespace
                f"OR namespace LIKE '{namespace}:%';"
            )
            for table in tables
        )
        result = subprocess.run(
            (
                "docker",
                "exec",
                self.name,
                "psql",
                "-U",
                "cache",
                "-d",
                "mindroom",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                statements,
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            msg = f"failed to clear synthetic stress cache namespace: {result.stderr}"
            raise RuntimeError(msg)

    def wait_for_cached_event(
        self,
        *,
        base_namespace: str,
        principal_id: str,
        room_id: str,
        event_id: str,
        timeout_seconds: float,
    ) -> None:
        """Wait until one exact sync event reaches a principal-scoped cache."""
        if not self._started:
            msg = "cannot inspect a stress PostgreSQL namespace before startup"
            raise RuntimeError(msg)
        principal_digest = hashlib.sha256(principal_id.encode()).hexdigest()
        namespace = f"{base_namespace}:principal:{principal_digest}"
        query = (
            "SELECT EXISTS("
            "SELECT 1 FROM mindroom_event_cache_events "
            "WHERE namespace = :'namespace' AND room_id = :'room_id' AND event_id = :'event_id'"
            ");"
        )
        deadline = time.monotonic() + timeout_seconds
        while True:
            result = subprocess.run(
                (
                    "docker",
                    "exec",
                    "--interactive",
                    self.name,
                    "psql",
                    "-U",
                    "cache",
                    "-d",
                    "mindroom",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-v",
                    f"namespace={namespace}",
                    "-v",
                    f"room_id={room_id}",
                    "-v",
                    f"event_id={event_id}",
                    "-At",
                ),
                check=False,
                capture_output=True,
                input=query,
                text=True,
                timeout=30,
            )
            if result.returncode:
                msg = f"failed to inspect synthetic stress cache namespace: {result.stderr}"
                raise RuntimeError(msg)
            if result.stdout.strip() == "t":
                return
            if time.monotonic() >= deadline:
                msg = "stress sync fence did not reach the agent cache before timeout"
                raise TimeoutError(msg)
            time.sleep(0.1)

    def close(self) -> None:
        """Remove only the exact owned PostgreSQL container."""
        if not self._started:
            return
        subprocess.run(
            ("docker", "rm", "--force", "--volumes", self.name),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self._started = False


def expected_minimum_matrix_edits(config: StressConfig) -> int:
    """Derive a conservative edit floor from production throttle cadence.

    Streaming begins near 0.5 seconds and ramps to a five-second steady state.
    Requiring one edit per five seconds proves repeated real replacements while
    allowing intentional character and cache-write coalescing.
    """
    return max(2, math.floor(config.stream_seconds / 5.0))


def matrix_edit_activity(
    edits_by_stream: Mapping[str, Sequence[float]],
) -> dict[str, object]:
    """Return a relative timeline of simultaneously active Matrix edit streams."""
    intervals = [(min(edits), max(edits)) for edits in edits_by_stream.values() if edits]
    edit_times = sorted({time_value for edits in edits_by_stream.values() for time_value in edits})
    origin = edit_times[0] if edit_times else 0.0
    timeline = [
        {
            "offset_seconds": round(timestamp - origin, 3),
            "active_streams": sum(start <= timestamp <= end for start, end in intervals),
        }
        for timestamp in edit_times
    ]
    return {
        "max_active_streams": max((int(point["active_streams"]) for point in timeline), default=0),
        "timeline": timeline,
    }


def assert_matrix_edit_shape(
    config: StressConfig,
    edits_by_stream: Mapping[str, Sequence[float]],
) -> dict[str, object]:
    """Fail unless every stream produced repeated overlapping Matrix edits."""
    expected_streams = config.threads * config.waves
    if len(edits_by_stream) != expected_streams:
        msg = f"Matrix edit evidence covers {len(edits_by_stream)}/{expected_streams} streams"
        raise AssertionError(msg)
    minimum = expected_minimum_matrix_edits(config)
    sparse = {label: len(edits) for label, edits in edits_by_stream.items() if len(edits) < minimum}
    if sparse:
        msg = f"insufficient Matrix edit activity; minimum={minimum}, streams={sparse}"
        raise AssertionError(msg)
    activity = matrix_edit_activity(edits_by_stream)
    simultaneous = int(activity["max_active_streams"])
    if simultaneous < config.threads:
        msg = f"Matrix edit overlap reached {simultaneous}/{config.threads} streams"
        raise AssertionError(msg)
    return activity


def write_replay_command(
    *,
    scenario_path: str = "scenario.json",
    artifact_root: str = "artifacts/live-matrix-stress",
) -> str:
    """Return a repository-relative replay command without host-specific paths."""
    return (
        "uv run python scripts/testing/fuzz_live_matrix.py "
        f"--profile stress --trace {scenario_path} --artifact-root {artifact_root} "
        "--nio-overlay <clean-mindroom-nio-checkout>\n"
    )


__all__ = [
    "DEFAULT_STRESS_ARTIFACT_ROOT",
    "ArtifactSanitizer",
    "BaselineSample",
    "ManagedStressPostgres",
    "ResourceSample",
    "StressArtifactBundle",
    "StressBaseline",
    "StressConfig",
    "StressLogMetrics",
    "StressRequest",
    "SyntheticStressAudit",
    "aggregate_log_metrics",
    "assert_matrix_edit_shape",
    "assert_resource_health",
    "current_machine_class",
    "expected_minimum_matrix_edits",
    "latency_summary",
    "matrix_edit_activity",
    "parse_structured_log",
    "percentile",
    "resource_summary",
    "write_replay_command",
]
