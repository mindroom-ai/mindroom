"""Deterministic unit and fault-injection tests for the live Matrix stress framework."""

# Test names state their behavior more precisely than repeated function docstrings.
# ruff: noqa: D103

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts.testing.live_matrix_stress import (
    ArtifactSanitizer,
    BaselineSample,
    ManagedStressPostgres,
    ResourceSample,
    StressArtifactBundle,
    StressBaseline,
    StressConfig,
    StressLogMetrics,
    StressRequest,
    SyntheticStressAudit,
    aggregate_log_metrics,
    assert_matrix_edit_shape,
    assert_resource_health,
    current_machine_class,
    expected_minimum_matrix_edits,
    latency_summary,
    percentile,
    resource_summary,
    write_replay_command,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def _sample(multiplier: float = 1.0) -> BaselineSample:
    return BaselineSample(
        source_to_final_p95_ms=1000 * multiplier,
        source_to_final_p99_ms=1200 * multiplier,
        throughput_responses_per_second=10 / multiplier,
    )


def _stable_baseline() -> StressBaseline:
    return StressBaseline(
        profile="stress-50x45x2",
        source_revision="a" * 40,
        config_sha256="b" * 64,
        machine_class=current_machine_class(),
        samples=(_sample(0.98), _sample(1.0), _sample(1.02)),
    )


def test_stress_profile_defaults_and_trace_round_trip() -> None:
    config = StressConfig()

    assert config.threads == 100
    assert config.rooms == 10
    assert config.stream_seconds == 45
    assert config.edit_interval == 0.5
    assert config.chars_per_second == 80
    assert config.min_response_chars == 1800
    assert config.max_response_chars == 3600
    assert config.chunk_chars == 40
    assert config.tool_call_probability == 1
    assert config.waves == 2
    assert config.cache_backend == "postgres"
    assert config.fault_mode == "none"
    assert StressConfig.from_json(config.to_json()) == config


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"threads": 0}, "threads must be positive"),
        ({"threads": 2, "rooms": 3}, "rooms cannot exceed threads"),
        ({"cache_backend": "sqlite"}, "requires PostgreSQL"),
        ({"stream_seconds": 1.1, "edit_interval": 0.5}, "exactly divisible"),
        ({"history_turns": -1}, "history_turns must be non-negative"),
        ({"fault_mode": "unknown"}, "unsupported stress fault mode"),
    ],
)
def test_stress_config_rejects_invalid_shapes(changes: dict[str, object], message: str) -> None:
    config = replace(StressConfig(), **changes)

    with pytest.raises(ValueError, match=message):
        config.validate()


def test_stress_marker_and_request_label() -> None:
    config = StressConfig(threads=2, rooms=1, waves=1, stream_seconds=1, edit_interval=0.5, seed=17)

    marker = config.marker(0, 1)

    assert marker == "LMS[wave=0;thread=001;seed=17]"
    assert StressRequest(wave=0, thread=1, seed=17).label == "wave-00/thread-001"


def test_serialized_stream_fault_round_trips_in_replay_config() -> None:
    config = StressConfig(fault_mode="serialize-streams")

    assert StressConfig.from_json(config.to_json()).fault_mode == "serialize-streams"


def test_synthetic_stress_audit_requires_real_tool_continuation(tmp_path: Path) -> None:
    config = StressConfig(
        threads=1,
        rooms=1,
        waves=1,
        stream_seconds=2,
        edit_interval=0.5,
        tool_call_probability=1,
    )
    telemetry_path = tmp_path / "synthetic.jsonl"
    audit = SyntheticStressAudit(config, telemetry_path)
    request = StressRequest(wave=0, thread=0, seed=config.seed)
    plan = audit.plan(request)
    events = [
        {"kind": "barrier_reached", "phase": "initial", "group": "0", "time": 1.0},
        {"kind": "request_started", "phase": "initial", "group": "0", "time": 2.0},
        {"kind": "tool_call_emitted", "phase": "initial", "group": "0", "time": 3.0},
        {"kind": "request_finished", "phase": "initial", "group": "0", "time": 4.0},
        {"kind": "request_started", "phase": "continuation", "group": "0", "time": 5.0},
        {"kind": "request_finished", "phase": "continuation", "group": "0", "time": 6.0},
    ]
    telemetry_path.write_text(
        "".join(json.dumps({"request_id": plan.request_id, **event}) + "\n" for event in events),
        encoding="utf-8",
    )

    audit.assert_complete()
    assert audit.expected_body(request) == plan.body
    assert audit.expected_matrix_body(request) == plan.prefix + "\n\n🔧 `sleep` [1]\n\n" + plan.suffix
    assert audit.snapshot()["tool_calls"] == 1

    telemetry_path.write_text(
        "".join(
            json.dumps({"request_id": plan.request_id, **event}) + "\n"
            for event in events
            if event["phase"] != "continuation"
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="request_started:continuation count 0/1"):
        audit.assert_complete()


def test_latency_percentiles_are_interpolated() -> None:
    values = [1, 2, 3, 4, 5]

    assert percentile(values, 50) == 3
    assert percentile(values, 90) == pytest.approx(4.6)
    assert latency_summary(values) == {
        "count": 5,
        "p50": 3.0,
        "p90": 4.6,
        "p95": 4.8,
        "p99": 4.96,
        "max": 5,
    }
    assert latency_summary([]) == {"count": 0}


def test_baseline_round_trip_and_candidate_comparison() -> None:
    baseline = _stable_baseline()

    loaded = StressBaseline.from_json(baseline.to_json())
    comparison = loaded.compare(_sample(1.1))

    assert comparison["passed"] is True
    assert comparison["allowance"] == 0.25
    assert comparison["baseline_medians"]["source_to_final_p95_ms"] == 1000


def test_baseline_regression_gate_fails_when_enforced() -> None:
    baseline = _stable_baseline()

    with pytest.raises(AssertionError, match="stress performance regression"):
        baseline.compare(_sample(1.4))


def test_baseline_regression_can_be_observed_without_enforcement() -> None:
    comparison = _stable_baseline().compare(_sample(1.4), enforce=False)

    assert comparison["passed"] is False
    assert comparison["regressed_metrics"] == [
        "source_to_final_p95_ms",
        "source_to_final_p99_ms",
        "throughput_responses_per_second",
    ]


def test_baseline_rejects_different_machine_class() -> None:
    baseline = _stable_baseline()

    with pytest.raises(ValueError, match="machine class mismatch"):
        baseline.compare(_sample(), machine_class="different-machine")


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"version": 1, "profile": "stress"}',
        (
            '{"version": 1, "profile": "stress", "source_revision": "a", '
            '"config_sha256": "b", "samples": [{"source_to_final_p95_ms": 1}]}'
        ),
    ],
)
def test_malformed_or_missing_baseline_fails(payload: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        StressBaseline.from_json(payload)


def test_high_variance_baseline_is_rejected() -> None:
    baseline = StressBaseline(
        profile="stress",
        source_revision="a",
        config_sha256="b",
        machine_class=current_machine_class(),
        samples=(_sample(0.5), _sample(1), _sample(1.5)),
    )

    with pytest.raises(ValueError, match="fix determinism before gating"):
        baseline.validate()


def test_log_metrics_aggregate_cache_write_and_health_signals() -> None:
    text = "\n".join(
        [
            json.dumps(
                {
                    "event": "Event cache outbound schedule timing",
                    "barrier_kind": "thread",
                    "is_edit": True,
                },
            ),
            json.dumps(
                {
                    "event": "Event cache update timing",
                    "predecessor_wait_ms": 3,
                    "update_run_ms": 5,
                    "total_ms": 8,
                    "coalesced_update_count": 4,
                    "outcome": "ok",
                },
            ),
            json.dumps(
                {
                    "event": "matrix_cache_thread_history_refreshed",
                    "mode": "cache_hit",
                },
            ),
            json.dumps(
                {
                    "event": "Thread history cache store completed",
                    "cache_store_outcome": "not_replaced",
                },
            ),
            json.dumps(
                {
                    "event": "outbound_thread_reservation",
                    "action": "released",
                    "active_count": 0,
                },
            ),
        ],
    )

    metrics = aggregate_log_metrics(text)
    summary = metrics.summary()

    assert summary["barriers"] == {
        "room": 0,
        "thread": 1,
        "known_thread_room_violations": 0,
    }
    assert summary["write_coordination"]["coalesced_intermediate_writes"] == 4
    assert summary["cache"]["hits"] == 1
    assert summary["cache"]["snapshot_store_outcomes"] == {"not_replaced": 1}
    assert summary["reservations"]["released"] == 1
    metrics.assert_healthy()


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (
            {
                "event": "Event cache outbound schedule timing",
                "barrier_kind": "room",
                "is_edit": True,
            },
            "known-thread edits used room barriers",
        ),
        (
            {
                "event": "outbound_thread_reservation",
                "action": "created",
                "active_count": 1,
            },
            "thread reservations leaked",
        ),
        ({"event": "event_loop_stall_detected"}, "watchdog stalls"),
        ({"event": "sync_restart_retry_started"}, "sync-restart retries"),
        ({"event": "Response was interrupted"}, "interruption signals"),
        ({"event": "Task exception was never retrieved"}, "uncaught exceptions"),
        ({"event": "another command is already in progress"}, "PostgreSQL connection-concurrency"),
    ],
)
def test_fault_signals_fail_health_gate(event: dict[str, object], message: str) -> None:
    metrics = StressLogMetrics()
    metrics.ingest(event)

    with pytest.raises(AssertionError, match=message):
        metrics.assert_healthy()


def test_duplicate_cache_repair_scan_fails_gate() -> None:
    metrics = StressLogMetrics()
    event = {
        "event": "matrix_cache_thread_history_refreshed",
        "mode": "full_scan",
        "thread_id": "thread-001",
    }

    metrics.ingest(event)
    metrics.ingest(event)

    with pytest.raises(AssertionError, match="duplicate repair scans"):
        metrics.assert_healthy()


def test_whole_run_health_can_exclude_intentional_cross_phase_repairs() -> None:
    metrics = StressLogMetrics()
    event = {
        "event": "matrix_cache_thread_history_refreshed",
        "mode": "full_scan",
        "thread_id": "thread-001",
    }
    metrics.ingest(event)
    metrics.ingest(event)

    metrics.assert_healthy(check_duplicate_repairs=False)


def test_artifact_sanitizer_redacts_secrets_urls_paths_and_matrix_ids() -> None:
    sanitizer = ArtifactSanitizer()
    raw = {
        "access_token": "syt-secret-token-value",
        "message": (
            "Authorization: Bearer raw-secret "
            "postgresql://cache:password@db.internal/mindroom "
            "https://private.example/path "
            "/Users/person/private/log "
            "!room:private.example $eventabcdef @person:private.example s123_longrawsynctoken"
        ),
    }

    sanitized = sanitizer.value(raw)
    serialized = json.dumps(sanitized)

    sanitizer.assert_clean(serialized)
    assert sanitized["access_token"] == "<redacted>"  # noqa: S105 - sanitizer sentinel
    assert "raw-secret" not in serialized
    assert "private.example" not in serialized
    assert "/Users/person" not in serialized
    assert "<room-001>" in serialized
    assert "<event-001>" in serialized
    assert "<user-001>" in serialized


def test_sanitizer_fault_injection_detects_unredacted_token() -> None:
    sanitizer = ArtifactSanitizer()

    with pytest.raises(AssertionError, match="sanitizer leaked"):
        sanitizer.assert_clean("Authorization: Bearer leaked-token")


def test_artifact_bundle_retains_success_and_failure_evidence() -> None:
    persistent_root = Path.cwd() / "artifacts" / "live-matrix-stress-tests"
    run_root = persistent_root / "run-001"
    if run_root.exists():
        shutil.rmtree(run_root)
    try:
        bundle = StressArtifactBundle.create(persistent_root, "run-001")

        summary_path = bundle.write_json(
            "summary.json",
            {
                "room_id": "!room:synthetic.invalid",
                "result": "PASS",
                "repair_fetches_by_thread": {"$event:synthetic.invalid": 1},
            },
        )
        failure_path = bundle.write_text(
            "failure.txt",
            "synthetic failure for $event:synthetic.invalid",
        )

        assert summary_path.exists()
        assert failure_path.exists()
        assert "!room:" not in summary_path.read_text(encoding="utf-8")
        assert "$event" not in failure_path.read_text(encoding="utf-8")
        assert "$event" not in summary_path.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(run_root, ignore_errors=True)


@pytest.mark.parametrize(
    "root",
    [
        Path("/tmp/live-matrix-stress"),  # noqa: S108 - path must be rejected
        Path("/private/tmp/live-matrix-stress"),
        Path("/var/tmp/live-matrix-stress"),  # noqa: S108 - path must be rejected
    ],
)
def test_artifact_bundle_rejects_temporary_roots(root: Path) -> None:
    with pytest.raises(ValueError, match="must be persistent"):
        StressArtifactBundle.create(root, "run")


def test_matrix_edit_shape_counts_and_overlap() -> None:
    config = StressConfig(threads=2, rooms=1, waves=1, stream_seconds=10, edit_interval=0.5)
    edits = {
        "wave-00/thread-000": [1.0, 5.0],
        "wave-00/thread-001": [2.0, 6.0],
    }

    assert expected_minimum_matrix_edits(config) == 2
    activity = assert_matrix_edit_shape(config, edits)

    assert activity["max_active_streams"] == 2
    assert activity["timeline"][0] == {"offset_seconds": 0.0, "active_streams": 1}


def test_matrix_edit_shape_faults_on_missing_final_edit() -> None:
    config = StressConfig(threads=2, rooms=1, waves=1, stream_seconds=10, edit_interval=0.5)

    with pytest.raises(AssertionError, match="insufficient Matrix edit activity"):
        assert_matrix_edit_shape(
            config,
            {
                "wave-00/thread-000": [1.0, 5.0],
                "wave-00/thread-001": [2.0],
            },
        )


def test_resource_summary_reports_cpu_rss_sync_and_health() -> None:
    samples = [
        ResourceSample(0, 10, 100, 0.2, 3, True, True),
        ResourceSample(1, 20, 200, 0.4, 5, False, True),
    ]

    summary = resource_summary(samples)

    assert summary["count"] == 2
    assert summary["cpu_percent"]["p50"] == 15
    assert summary["rss_bytes"]["max"] == 200
    assert summary["event_loop_probe_latency_ms"]["max"] == 5
    assert summary["mindroom_health_probe_failures"] == 0
    assert summary["tuwunel_unhealthy_samples"] == 1
    assert summary["postgres_unhealthy_samples"] == 0


@pytest.mark.parametrize(
    ("summary", "message"),
    [
        ({"mindroom_health_probe_failures": 1}, "MindRoom health probe failures"),
        ({"tuwunel_unhealthy_samples": 1}, "Tuwunel unhealthy samples"),
        ({"postgres_unhealthy_samples": 1}, "PostgreSQL unhealthy samples"),
        ({"event_loop_probe_latency_ms": {"max": 5001}}, "event-loop progress probe"),
        ({"sync_age_seconds": {"max": 121}}, "Matrix sync age"),
    ],
)
def test_resource_health_safety_ceilings_fail(summary: dict[str, object], message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        assert_resource_health(summary)


def test_managed_postgres_preflight_never_falls_back(mocker: MockerFixture) -> None:
    completed = mocker.Mock(returncode=0, stdout="127.0.0.1:54321\n", stderr="")
    run = mocker.patch("scripts.testing.live_matrix_stress.subprocess.run", return_value=completed)
    postgres = ManagedStressPostgres("synthetic-postgres")

    postgres.start()

    assert postgres.host_port == 54321
    assert postgres.database_url == "postgresql://cache:synthetic-stress-password@127.0.0.1:54321/mindroom"
    assert any(call.args[0][:3] == ("docker", "run", "--detach") for call in run.call_args_list)
    assert postgres.is_healthy()
    postgres.close()
    assert any(call.args[0][:3] == ("docker", "rm", "--force") for call in run.call_args_list)


def test_stress_cache_clear_covers_principal_scoped_namespaces(mocker: MockerFixture) -> None:
    completed = mocker.Mock(returncode=0, stdout="", stderr="")
    run = mocker.patch("scripts.testing.live_matrix_stress.subprocess.run", return_value=completed)
    postgres = ManagedStressPostgres("synthetic-postgres")
    postgres._started = True

    postgres.clear_cache_namespace("synthetic")

    sql = run.call_args.args[0][-1]
    assert "namespace = 'synthetic' OR namespace LIKE 'synthetic:%'" in sql
    assert sql.count("DELETE FROM") == 9


def test_stress_sync_fence_waits_for_exact_principal_event(mocker: MockerFixture) -> None:
    completed = mocker.Mock(returncode=0, stdout="t\n", stderr="")
    run = mocker.patch("scripts.testing.live_matrix_stress.subprocess.run", return_value=completed)
    postgres = ManagedStressPostgres("synthetic-postgres")
    postgres._started = True

    postgres.wait_for_cached_event(
        base_namespace="synthetic",
        principal_id="@mindroom_general_synthetic:localhost",
        room_id="!room:localhost",
        event_id="$event",
        timeout_seconds=1,
    )

    command = run.call_args.args[0]
    assert command[:4] == ("docker", "exec", "--interactive", "synthetic-postgres")
    assert any(field.startswith("namespace=synthetic:principal:") for field in command)
    assert "room_id=!room:localhost" in command
    assert "event_id=$event" in command
    assert (
        "WHERE namespace = :'namespace' AND room_id = :'room_id' AND event_id = :'event_id'"
        in run.call_args.kwargs["input"]
    )


def test_replay_command_uses_only_repository_relative_paths() -> None:
    command = write_replay_command()

    assert "scripts/testing/fuzz_live_matrix.py" in command
    assert "artifacts/live-matrix-stress" in command
    assert "/Users/" not in command
