"""GPT-Live call telemetry reports cadence, backlog, delegation timing, and reply routing."""

from __future__ import annotations

import base64

import pytest
from structlog.testing import capture_logs

from mindroom.matrix_rtc.call_telemetry import (
    LiveCallTelemetry,
    _base64_pcm16_samples,
    _InputTranscriptCadence,
    _OutputAudioCadence,
)

_100_MS = 2400


def _events(logs: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == name]


@pytest.mark.parametrize("samples", [0, 1, 2, 3, 1200, 2401])
def test_base64_sample_count_matches_decoded_pcm(samples: int) -> None:
    """Counting from the encoded length avoids decoding every provider delta."""
    encoded = base64.b64encode(bytes(samples * 2)).decode()
    assert _base64_pcm16_samples(encoded) == samples


def test_output_bursts_split_on_gaps_and_report_how_far_audio_ran_ahead() -> None:
    """Audio arriving at twice real time builds a lead; a 300 ms pause starts a new burst."""
    cadence = _OutputAudioCadence()
    with capture_logs() as logs:
        for now in (10.0, 10.05, 10.1, 10.15):
            cadence.observe(_100_MS, now)
        cadence.observe(_100_MS, 10.5)
        cadence.close()

    first, second = _events(logs, "call_output_audio_burst")
    assert first == {
        "event": "call_output_audio_burst",
        "log_level": "info",
        "burst_index": 0,
        "audio_ms": 400.0,
        "wall_ms": 150.0,
        "max_ahead_ms": 250.0,
        "delta_count": 4,
        "gap_before_ms": None,
        "final": True,
    }
    assert (second["burst_index"], second["audio_ms"], second["wall_ms"]) == (1, 100.0, 0.0)
    assert (second["max_ahead_ms"], second["gap_before_ms"]) == (100.0, 350.0)


def test_output_burst_reports_cumulative_progress_while_audio_never_pauses() -> None:
    """A continuous stream still logs once a minute without resetting its accounting."""
    cadence = _OutputAudioCadence()
    with capture_logs() as logs:
        for tick in range(611):
            cadence.observe(_100_MS, tick / 10)
        cadence.close()

    interim, final = _events(logs, "call_output_audio_burst")
    assert (interim["final"], interim["burst_index"], interim["wall_ms"]) == (False, 0, 60000.0)
    assert interim["max_ahead_ms"] == pytest.approx(100.0)
    assert (final["final"], final["burst_index"], final["audio_ms"], final["delta_count"]) == (True, 0, 61100.0, 611)


def test_input_transcript_cadence_reports_inter_arrival_percentiles_and_turn_closures() -> None:
    """One end-of-call record summarizes transcript delta spacing and SDK turn closures."""
    cadence = _InputTranscriptCadence()
    with capture_logs() as logs:
        for now in (1.0, 1.1, 1.3, 1.6, 2.6):
            cadence.observe_delta(now)
        cadence.observe_turn_closed()
        cadence.close()

    assert _events(logs, "call_input_transcript_cadence") == [
        {
            "event": "call_input_transcript_cadence",
            "log_level": "info",
            "count": 5,
            "p50_ms": 200.0,
            "p95_ms": 1000.0,
            "max_ms": 1000.0,
            "user_turn_closures": 1,
        },
    ]


def test_input_transcript_cadence_without_deltas_reports_no_percentiles() -> None:
    """A call without caller speech still reports its closure count."""
    cadence = _InputTranscriptCadence()
    with capture_logs() as logs:
        cadence.close()

    [entry] = _events(logs, "call_input_transcript_cadence")
    assert (entry["count"], entry["p50_ms"], entry["max_ms"], entry["user_turn_closures"]) == (0, None, None, 0)


def test_assistant_reply_is_delegated_only_after_new_commentary() -> None:
    """Each reply consumes the commentary appended since the previous reply."""
    telemetry = LiveCallTelemetry()
    telemetry.delegation_created("one")
    with capture_logs() as logs:
        telemetry.assistant_reply(12, started_at=0.0, interrupted=False)
        telemetry.commentary_appended("one", chunk_index=0, chunk_count=1)
        telemetry.assistant_reply(40, started_at=telemetry._last_commentary_at + 0.25, interrupted=True)
        telemetry.assistant_reply(8, started_at=telemetry._last_commentary_at + 5, interrupted=False)

    replies = _events(logs, "call_assistant_reply")
    assert [(entry["delegated"], entry["delegation_id"], entry["reply_chars"]) for entry in replies] == [
        (False, None, 12),
        (True, "one", 40),
        (False, None, 8),
    ]
    assert replies[0]["since_commentary_ms"] is None
    assert replies[1]["since_commentary_ms"] == 250.0
    assert replies[1]["interrupted"] is True
