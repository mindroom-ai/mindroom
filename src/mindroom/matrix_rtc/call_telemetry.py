"""Measurement-only telemetry for one GPT-Live call.

It observes raw provider events and MindRoom's delegation steps and logs
bounded summaries that attribute latency, audio breakups, and barge-in
failures. Nothing here changes audio, gating, buffering, or delegation.
"""

from __future__ import annotations

import base64
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.logging_config import get_logger
from mindroom.timing import elapsed_ms_between, elapsed_ms_since, nearest_rank_percentile

if TYPE_CHECKING:
    from mindroom.matrix_rtc.call_audio_capture import CallAudioCapture

logger = get_logger(__name__)

_SAMPLE_RATE = 24_000
#: Provider output deltas at least this far apart belong to separate bursts.
_OUTPUT_BURST_GAP_SECONDS = 0.3
#: A burst that never pauses still reports cumulative progress this often.
_OUTPUT_BURST_REPORT_SECONDS = 60.0

type _DelegationStage = Literal[
    "created",
    "enqueued",
    "lock_acquired",
    "response_done",
    "commentary_appended",
    "commentary_acked",
]


def _base64_pcm16_samples(data: str) -> int:
    """Count mono PCM16 samples in base64 text without decoding it."""
    padding = 2 if data.endswith("==") else int(data.endswith("="))
    return (len(data) * 3 // 4 - padding) // 2


@dataclass
class _OutputBurst:
    """Running totals for one burst of provider output deltas."""

    index: int
    started_at: float
    last_at: float
    next_report_at: float
    gap_before_ms: float | None
    samples: int = 0
    delta_count: int = 0
    max_ahead_ms: float = 0.0


class _OutputAudioCadence:
    """Group provider output deltas into bursts and track how far audio ran ahead of wall time.

    A lead that grows during a burst means the provider sends audio faster than
    real time and the excess waits in local buffers until it plays.
    """

    def __init__(self) -> None:
        self._burst: _OutputBurst | None = None
        self._burst_count = 0
        self._previous_end: float | None = None

    def observe(self, samples: int, now: float) -> None:
        """Account one provider output delta that arrived at monotonic time ``now``."""
        burst = self._burst
        if burst is not None and now - burst.last_at >= _OUTPUT_BURST_GAP_SECONDS:
            self.close()
            burst = None
        if burst is None:
            burst = self._burst = _OutputBurst(
                index=self._burst_count,
                started_at=now,
                last_at=now,
                next_report_at=now + _OUTPUT_BURST_REPORT_SECONDS,
                gap_before_ms=None if self._previous_end is None else elapsed_ms_between(self._previous_end, now),
            )
            self._burst_count += 1
        burst.last_at = now
        burst.samples += samples
        burst.delta_count += 1
        ahead_ms = burst.samples * 1000 / _SAMPLE_RATE - (now - burst.started_at) * 1000
        burst.max_ahead_ms = max(burst.max_ahead_ms, ahead_ms)
        if now >= burst.next_report_at:
            burst.next_report_at = now + _OUTPUT_BURST_REPORT_SECONDS
            _log_output_burst(burst, final=False)

    def close(self) -> None:
        """Report the open burst as finished."""
        burst, self._burst = self._burst, None
        if burst is not None:
            self._previous_end = burst.last_at
            _log_output_burst(burst, final=True)


def _log_output_burst(burst: _OutputBurst, *, final: bool) -> None:
    logger.info(
        "call_output_audio_burst",
        burst_index=burst.index,
        audio_ms=round(burst.samples * 1000 / _SAMPLE_RATE, 1),
        wall_ms=elapsed_ms_between(burst.started_at, burst.last_at),
        max_ahead_ms=round(burst.max_ahead_ms, 1),
        delta_count=burst.delta_count,
        gap_before_ms=burst.gap_before_ms,
        final=final,
    )


class _InputTranscriptCadence:
    """Summarize caller transcript delta spacing and SDK user-turn closures for one call."""

    def __init__(self) -> None:
        self._last_at: float | None = None
        self._intervals_ms: list[float] = []
        self._count = 0
        self._closures = 0

    def observe_delta(self, now: float) -> None:
        """Account one non-empty ``session.input_transcript.delta`` at monotonic time ``now``."""
        if self._last_at is not None:
            self._intervals_ms.append(elapsed_ms_between(self._last_at, now))
        self._last_at = now
        self._count += 1

    def observe_turn_closed(self) -> None:
        """Account one SDK user-turn closure."""
        self._closures += 1

    def close(self) -> None:
        """Log the whole-call summary."""
        intervals = sorted(self._intervals_ms)
        logger.info(
            "call_input_transcript_cadence",
            count=self._count,
            p50_ms=nearest_rank_percentile(intervals, 50) if intervals else None,
            p95_ms=nearest_rank_percentile(intervals, 95) if intervals else None,
            max_ms=intervals[-1] if intervals else None,
            user_turn_closures=self._closures,
        )


@dataclass
class _DelegationTiming:
    created_at: float
    enqueued_at: float | None = None


@dataclass(frozen=True, slots=True)
class _CommentaryChunk:
    delegation_id: str
    chunk_index: int
    chunk_count: int


class LiveCallTelemetry:
    """Collect one GPT-Live call's measurements from provider events and delegation steps.

    Commentary acknowledgements are matched to appends through the client event
    ID the SDK assigns when it sends each append. When an acknowledgement has no
    ``client_event_id``, it is matched to the oldest unacknowledged append.
    """

    def __init__(self, capture: CallAudioCapture | None = None) -> None:
        self._capture = capture
        self._output = _OutputAudioCadence()
        self._input = _InputTranscriptCadence()
        self._delegations: dict[str, _DelegationTiming] = {}
        # Delegation commentary appended locally that the SDK has not sent yet, in call order.
        self._unsent: deque[_CommentaryChunk] = deque()
        # Every sent commentary append awaiting its acknowledgement, keyed by client event ID in send
        # order; None marks commentary MindRoom did not delegate, such as the greeting.
        self._unacked: dict[str, _CommentaryChunk | None] = {}
        self._commentary_since_reply: str | None = None
        self._last_commentary_at: float | None = None

    def delegation_created(self, delegation_id: str) -> None:
        """Start timing an accepted delegation."""
        self._delegations[delegation_id] = _DelegationTiming(created_at=time.monotonic())
        self._log_stage(delegation_id, "created")

    def delegation_enqueued(self, delegation_id: str) -> None:
        """Note that the delegation is waiting for the delegate lock."""
        if (timing := self._delegations.get(delegation_id)) is not None:
            timing.enqueued_at = time.monotonic()
        self._log_stage(delegation_id, "enqueued")

    def delegation_lock_acquired(self, delegation_id: str) -> None:
        """Note that earlier delegations released the delegate."""
        timing = self._delegations.get(delegation_id)
        if timing is None or timing.enqueued_at is None:
            return
        self._log_stage(
            delegation_id,
            "lock_acquired",
            queue_wait_ms=elapsed_ms_since(timing.enqueued_at),
        )

    def delegation_response_done(self, delegation_id: str, response_chars: int) -> None:
        """Note that the delegate returned its complete answer."""
        self._log_stage(delegation_id, "response_done", response_chars=response_chars)

    def commentary_appended(self, delegation_id: str, *, chunk_index: int, chunk_count: int) -> None:
        """Note one commentary chunk handed to the SDK for this delegation."""
        self._unsent.append(_CommentaryChunk(delegation_id, chunk_index, chunk_count))
        self._commentary_since_reply = delegation_id
        self._last_commentary_at = time.time()
        self._log_stage(delegation_id, "commentary_appended", chunk_index=chunk_index, chunk_count=chunk_count)

    def assistant_reply(self, reply_chars: int, *, started_at: float, interrupted: bool) -> None:
        """Attribute one spoken assistant message to delegation or to GPT-Live itself.

        ``started_at`` is the message's wall-clock start. ``delegated`` means
        commentary was appended after the previous assistant message was recorded.
        """
        delegation_id, self._commentary_since_reply = self._commentary_since_reply, None
        logger.info(
            "call_assistant_reply",
            delegated=delegation_id is not None,
            delegation_id=delegation_id,
            reply_chars=reply_chars,
            interrupted=interrupted,
            since_commentary_ms=(
                None if self._last_commentary_at is None else elapsed_ms_between(self._last_commentary_at, started_at)
            ),
        )

    def on_client_event(self, event: dict[str, Any]) -> None:
        """Learn the client event ID the SDK sent for each commentary append."""
        if event.get("type") != "session.commentary.append":
            return
        chunk = self._unsent.popleft() if event.get("delegation_id") is not None and self._unsent else None
        self._unacked[str(event.get("event_id"))] = chunk

    def on_server_event(self, event: dict[str, Any]) -> None:
        """Observe one raw provider event before the SDK handles it."""
        event_type = event.get("type")
        if event_type == "session.output_audio.delta":
            if delta := event.get("delta") or "":
                self._output.observe(_base64_pcm16_samples(delta), time.monotonic())
                if self._capture is not None:
                    self._capture.provider_out(base64.b64decode(delta))
        elif event_type == "session.input_transcript.delta":
            if event.get("delta"):
                self._input.observe_delta(time.monotonic())
        elif event_type == "session.commentary.appended":
            self._on_commentary_acked(event)

    def on_input_speech_stopped(self, _event: object) -> None:
        """Count one SDK user-turn closure."""
        self._input.observe_turn_closed()

    async def aclose(self) -> None:
        """Log end-of-call summaries and finish any audio capture."""
        self._output.close()
        self._input.close()
        if self._capture is not None:
            await self._capture.aclose()

    def _on_commentary_acked(self, event: dict[str, Any]) -> None:
        client_event_id = event.get("client_event_id")
        if isinstance(client_event_id, str):
            correlation = "client_event_id"
            chunk = self._unacked.pop(client_event_id, None)
        elif self._unacked:
            correlation = "order"
            chunk = self._unacked.pop(next(iter(self._unacked)))
        else:
            return
        if chunk is None:
            return
        self._log_stage(
            chunk.delegation_id,
            "commentary_acked",
            chunk_index=chunk.chunk_index,
            chunk_count=chunk.chunk_count,
            correlation=correlation,
        )

    def _log_stage(self, delegation_id: str, stage: _DelegationStage, **fields: object) -> None:
        if (timing := self._delegations.get(delegation_id)) is None:
            return
        logger.info(
            "call_delegation_stage",
            delegation_id=delegation_id,
            stage=stage,
            elapsed_ms=elapsed_ms_since(timing.created_at),
            **fields,
        )
