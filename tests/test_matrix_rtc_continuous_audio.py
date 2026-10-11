"""GPT-Live audio stays real-time across microphone gaps and output stalls."""

from __future__ import annotations

import asyncio
from itertools import pairwise
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit import rtc
from livekit.agents import room_io
from livekit.agents.voice import io as agents_io
from livekit.rtc._utils import RingQueue
from structlog.testing import capture_logs

import mindroom.matrix_rtc.voice_agent as voice_agent_module
from mindroom.matrix_rtc.voice_agent import (
    LiveVoiceAgentOptions,
    RealtimeVoiceBridge,
    VoiceAgentOptions,
    _AuthorizedParticipantAudioInput,
    _bound_live_output_lag,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class Microphone:
    """A controllable external microphone stream with observable read lifetime."""

    def __init__(self) -> None:
        self.frames: asyncio.Queue[rtc.AudioFrame] = asyncio.Queue()
        self.reading = asyncio.Event()
        self.closed = False
        self.pending_reads = 0

    async def __anext__(self) -> SimpleNamespace:
        """Wait for one external audio frame and expose pending reads."""
        self.pending_reads += 1
        self.reading.set()
        try:
            return SimpleNamespace(frame=await self.frames.get())
        finally:
            self.pending_reads -= 1

    async def aclose(self) -> None:
        """Record closure of the external stream."""
        self.closed = True


async def start_audio(
    *,
    live: bool = True,
    stream_factory: Callable[..., object] | None = None,
    session: MagicMock | None = None,
) -> tuple[_AuthorizedParticipantAudioInput, MagicMock]:
    """Exercise session input wiring with the real RTC mixer and no network."""
    room = MagicMock()
    room.remote_participants = {}
    room.local_participant.track_publications = {}
    bridge = RealtimeVoiceBridge(local_identity="bot", e2ee_enabled=False)
    bridge._room = room
    bridge._participant_identities = frozenset({"alice"})
    if session is None:
        session = MagicMock()
    session.start = AsyncMock()
    options = (
        LiveVoiceAgentOptions(
            get_instructions=AsyncMock(return_value="Speak."),
            model="gpt-live-1",
            api_key="test",
            voice="marin",
            respond=AsyncMock(),
        )
        if live
        else VoiceAgentOptions(instructions="Speak.", model="realtime", api_key="test")
    )
    rtc_module = cast(
        "ModuleType",
        SimpleNamespace(
            AudioMixer=rtc.AudioMixer,
            AudioFrame=rtc.AudioFrame,
            AudioStream=stream_factory or (lambda track, **_kwargs: track),
            TrackKind=rtc.TrackKind,
            TrackSource=rtc.TrackSource,
        ),
    )
    await bridge._start_session(
        session,
        MagicMock(),
        options,
        rtc_module=rtc_module,
        room_io_module=room_io,
    )
    return session.input.audio, room


def subscribe(room: MagicMock, identity: str, microphone: Microphone) -> None:
    """Deliver the same subscription event emitted by the RTC room."""
    publication = SimpleNamespace(
        sid=f"{identity}-mic",
        kind=rtc.TrackKind.KIND_AUDIO,
        source=rtc.TrackSource.SOURCE_MICROPHONE,
        set_subscribed=MagicMock(),
    )
    participant = SimpleNamespace(identity=identity)
    for call in room.on.call_args_list:
        if call.args[0] == "track_subscribed":
            callback = cast("Callable[..., None]", call.args[1])
            callback(microphone, publication, participant)


def frame(value: int) -> rtc.AudioFrame:
    """Use distinct fixed PCM samples to detect duplicates and dropped frames."""
    return rtc.AudioFrame(value.to_bytes(2, "little", signed=True) * 1200, 24000, 1, 1200)


@pytest.mark.asyncio
async def test_live_input_emits_paced_silence_before_any_participant_joins() -> None:
    """Starting with an empty RTC room must still deliver valid realtime PCM."""
    audio, _room = await start_audio()
    try:
        started = asyncio.get_running_loop().time()
        frames = [await asyncio.wait_for(anext(audio), timeout=1) for _ in range(4)]
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed >= 0.15
        assert elapsed < 0.8
        for item in frames:
            assert (item.sample_rate, item.num_channels, item.samples_per_channel) == (24000, 1, 1200)
            assert item.data.tobytes() == bytes(2400)
    finally:
        await audio.aclose()


@pytest.mark.asyncio
async def test_live_input_resumes_real_frames_after_late_microphone_and_stream_gap() -> None:
    """Late and resumed audio reaches the model once, in order, between silence frames."""
    audio, room = await start_audio()
    microphone = Microphone()
    try:
        assert (await asyncio.wait_for(anext(audio), timeout=1)).data.tobytes() == bytes(2400)
        subscribe(room, "alice", microphone)
        await asyncio.wait_for(microphone.reading.wait(), timeout=1)
        assert (await asyncio.wait_for(anext(audio), timeout=1)).data.tobytes() == bytes(2400)
        for value in (17, 29):
            microphone.frames.put_nowait(frame(value))
        received = []
        async with asyncio.timeout(2):
            while len(received) < 2:
                item = await anext(audio)
                if any(item.data):
                    received.append(item.data.tobytes())
        assert received == [frame(17).data.tobytes(), frame(29).data.tobytes()]
        for _ in range(3):
            assert (await asyncio.wait_for(anext(audio), timeout=1)).data.tobytes() == bytes(2400)
        microphone.frames.put_nowait(frame(43))
        async with asyncio.timeout(2):
            while not any((resumed := await anext(audio)).data):
                pass
        assert resumed.data.tobytes() == frame(43).data.tobytes()
        assert (await asyncio.wait_for(anext(audio), timeout=1)).data.tobytes() == bytes(2400)
    finally:
        await audio.aclose()
    assert microphone.closed
    assert microphone.pending_reads == 0


@pytest.mark.asyncio
async def test_live_silence_never_admits_an_unauthorized_microphone() -> None:
    """A remote track outside the roster cannot replace silence with its samples."""
    audio, room = await start_audio()
    microphone = Microphone()
    microphone.frames.put_nowait(frame(71))
    subscribe(room, "outsider", microphone)
    try:
        for _ in range(3):
            assert (await asyncio.wait_for(anext(audio), timeout=1)).data.tobytes() == bytes(2400)
        assert not microphone.reading.is_set()
        assert microphone.frames.qsize() == 1
    finally:
        await audio.aclose()


@pytest.mark.asyncio
async def test_live_cancelled_consumer_keeps_real_frame_and_close_ends_pending_read() -> None:
    """Cancelling one consumer cannot discard a mixer frame or leave a read after close."""
    audio, room = await start_audio()
    microphone = Microphone()
    subscribe(room, "alice", microphone)
    pending = asyncio.create_task(anext(audio))
    try:
        await asyncio.wait_for(microphone.reading.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        microphone.frames.put_nowait(frame(89))
        async with asyncio.timeout(2):
            while not any((item := await anext(audio)).data):
                pass
        assert item.data.tobytes() == frame(89).data.tobytes()
        assert (await asyncio.wait_for(anext(audio), timeout=1)).data.tobytes() == bytes(2400)
        pending = asyncio.create_task(anext(audio))
        await asyncio.sleep(0)
        await audio.aclose()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, timeout=1)
        with pytest.raises(StopAsyncIteration):
            await anext(audio)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await audio.aclose()
    assert microphone.closed
    assert microphone.pending_reads == 0


@pytest.mark.asyncio
async def test_other_voice_models_keep_waiting_for_real_audio() -> None:
    """Only GPT-Live receives synthesized silence at the shared session seam."""
    audio, _room = await start_audio(live=False)
    pending = asyncio.create_task(anext(audio))
    try:
        done, _ = await asyncio.wait({pending}, timeout=0.15)
        assert not done
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await audio.aclose()


@pytest.mark.asyncio
async def test_live_input_paces_buffered_real_frames_without_duplicates() -> None:
    """A ready microphone backlog cannot bypass the realtime output cadence."""
    audio, room = await start_audio()
    microphone = Microphone()
    for value in (11, 22, 33, 44):
        microphone.frames.put_nowait(frame(value))
    subscribe(room, "alice", microphone)
    received = []
    delivered_at = []
    try:
        async with asyncio.timeout(2):
            while len(received) < 4:
                item = await anext(audio)
                if any(item.data):
                    received.append(item.data.tobytes())
                    delivered_at.append(asyncio.get_running_loop().time())
        assert received == [frame(value).data.tobytes() for value in (11, 22, 33, 44)]
        assert all(after - before >= 0.045 for before, after in pairwise(delivered_at))
    finally:
        await audio.aclose()


@pytest.mark.asyncio
async def test_live_input_owns_one_stalled_mixer_read_until_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence ticks must neither cancel/restart the mixer read nor leak it at close."""
    pending_reads = 0
    read_calls = 0
    read_finished = asyncio.Event()
    original_next = rtc.AudioMixer.__anext__

    async def observed_next(mixer: rtc.AudioMixer) -> rtc.AudioFrame:
        nonlocal pending_reads, read_calls
        pending_reads += 1
        read_calls += 1
        try:
            return await original_next(mixer)
        finally:
            pending_reads -= 1
            read_finished.set()

    monkeypatch.setattr(rtc.AudioMixer, "__anext__", observed_next)
    audio, _room = await start_audio()
    try:
        for _ in range(3):
            assert (await asyncio.wait_for(anext(audio), timeout=1)).data.tobytes() == bytes(2400)
        assert pending_reads == 1
        assert read_calls == 1
        assert not read_finished.is_set()
    finally:
        await audio.aclose()
    assert pending_reads == 0
    assert read_finished.is_set()


@pytest.mark.asyncio
async def test_live_cancelled_close_drains_mixer_and_can_be_awaited_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation while draining a mixer read cannot skip the remaining teardown."""
    read_cancelled = asyncio.Event()
    release_read = asyncio.Event()
    mixer_closed = asyncio.Event()
    mixers = []
    original_next = rtc.AudioMixer.__anext__
    original_close = rtc.AudioMixer.aclose

    async def gated_next(mixer: rtc.AudioMixer) -> rtc.AudioFrame:
        mixers.append(mixer)
        try:
            return await original_next(mixer)
        finally:
            read_cancelled.set()
            await release_read.wait()

    async def observed_close(mixer: rtc.AudioMixer) -> None:
        await original_close(mixer)
        mixer_closed.set()

    monkeypatch.setattr(rtc.AudioMixer, "__anext__", gated_next)
    monkeypatch.setattr(rtc.AudioMixer, "aclose", observed_close)
    audio, _room = await start_audio()
    closing = None
    retry = None
    try:
        await asyncio.wait_for(anext(audio), timeout=1)
        closing = asyncio.create_task(audio.aclose())
        await asyncio.wait_for(read_cancelled.wait(), timeout=1)
        closing.cancel()
        done, _ = await asyncio.wait({closing}, timeout=0.05)
        assert not done, "Cancelled close abandoned mixer teardown"
        retry = asyncio.create_task(audio.aclose())
        done, _ = await asyncio.wait({retry}, timeout=0.05)
        assert not done, "Concurrent close did not join the owned teardown"
        release_read.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await retry
        assert mixer_closed.is_set()
    finally:
        release_read.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        if retry is not None:
            await asyncio.gather(retry, return_exceptions=True)
        await audio.aclose()
        for mixer in mixers:
            await original_close(mixer)


@pytest.mark.asyncio
async def test_live_audio_deadlines_absorb_jitter_and_rebase_after_long_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary late wakeups must not compound into latency, and a pause must not cause a burst."""
    audio, _room = await start_audio()
    clock = SimpleNamespace(now=0.0)

    class LateDeadline:
        def __init__(self, delay: float) -> None:
            self.delay = delay

        async def __aenter__(self) -> None:
            """Wake five milliseconds late without waiting for wall clock time."""
            clock.now += self.delay + 0.005
            raise TimeoutError

        async def __aexit__(self, *_args: object) -> None:
            """Satisfy the timeout context interface."""

    monkeypatch.setattr(
        voice_agent_module,
        "asyncio",
        SimpleNamespace(
            create_task=asyncio.create_task,
            gather=asyncio.gather,
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now),
            timeout=LateDeadline,
        ),
    )
    try:
        delivered_at = []
        for _ in range(40):
            assert (await anext(audio)).data.tobytes() == bytes(2400)
            delivered_at.append(clock.now)
        # 39 frame intervals, with at most one late wakeup rather than 39 of them.
        assert delivered_at[-1] - delivered_at[0] <= 1.956
        clock.now += 1.0
        await anext(audio)
        after_pause = clock.now
        await anext(audio)
        assert clock.now - after_pause >= 0.05
    finally:
        await audio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [True, False], ids=["live", "other-model"])
async def test_only_live_audio_bounds_upstream_backlog_to_recent_frames(live: bool) -> None:
    """A paused Live consumer retains recent audio instead of accumulating an unbounded backlog."""
    streams = []

    class BufferedMicrophone:
        def __init__(self, capacity: int) -> None:
            # Use the SDK's actual drop-oldest queue to exercise the configured bound.
            self.frames: RingQueue[rtc.AudioFrame] = RingQueue(capacity)

        async def __anext__(self) -> SimpleNamespace:
            """Deliver one queued microphone frame."""
            return SimpleNamespace(frame=await self.frames.get())

        async def aclose(self) -> None:
            """No external resources are held by the synthetic microphone."""

    def stream_factory(_track: object, *, capacity: int = 0, **_kwargs: object) -> BufferedMicrophone:
        stream = BufferedMicrophone(capacity)
        streams.append(stream)
        return stream

    audio, room = await start_audio(live=live, stream_factory=stream_factory)
    subscribe(room, "alice", Microphone())
    for value in range(1, 21):
        streams[0].frames.put(frame(value))
    expected_values = list(range(11, 21)) if live else list(range(1, 21))
    received = []
    try:
        async with asyncio.timeout(2):
            while len(received) < len(expected_values):
                item = await anext(audio)
                if any(item.data):
                    received.append(item.data.tobytes())
        assert received == [frame(value).data.tobytes() for value in expected_values]
    finally:
        await audio.aclose()


class OutputClock:
    """Stand in for the monotonic clock the output paces against."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the simulated time."""
        return self.now


@pytest.fixture
def output_clock(monkeypatch: pytest.MonkeyPatch) -> OutputClock:
    """Drive output arrival times without sleeping."""
    clock = OutputClock()
    monkeypatch.setattr(voice_agent_module, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock


class PlayoutSink(agents_io.AudioOutput):
    """Stand in for RoomIO: record deliveries and report each flushed segment as played."""

    def __init__(self, clock: OutputClock) -> None:
        super().__init__(
            label="RoomIO",
            capabilities=agents_io.AudioOutputCapabilities(pause=True),
            sample_rate=24000,
        )
        self.clock = clock
        self.deliveries: list[tuple[int, float]] = []
        self.calls: list[str] = []
        self.segment_open = False

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """Record one delivered delta and when it reached the publisher."""
        await super().capture_frame(frame)
        if not self.segment_open:
            self.segment_open = True
            self.on_playback_started(created_at=self.clock.now)
        self.deliveries.append((frame.data[0], self.clock.now))

    def flush(self) -> None:
        """Finish the open segment as fully played."""
        super().flush()
        self.calls.append("flush")
        if self.segment_open:
            self.segment_open = False
            self.on_playback_finished(playback_position=0.1, interrupted=False)

    def clear_buffer(self) -> None:
        """Record that queued audio was discarded."""
        self.calls.append("clear")

    def pause(self) -> None:
        """Record a pause request."""
        self.calls.append("pause")

    def resume(self) -> None:
        """Record a resume request."""
        self.calls.append("resume")

    @property
    def values(self) -> list[int]:
        """Return delivered delta identities in delivery order."""
        return [value for value, _at in self.deliveries]

    def queued_at_delivery(self) -> list[float]:
        """Return how much earlier audio each delivered delta waited behind in real-time playout."""
        queued = []
        playout_end = float("-inf")
        for _value, at in self.deliveries:
            queued.append(max(playout_end - at, 0.0))
            playout_end = max(playout_end, at) + 0.1
        return queued


def delta(value: int) -> rtc.AudioFrame:
    """Build one 100 ms GPT-Live output delta identified by its sample value."""
    return rtc.AudioFrame(value.to_bytes(2, "little", signed=True) * 2400, 24000, 1, 2400)


async def deliver(output: agents_io.AudioOutput, clock: OutputClock, value: int, *, at: float) -> None:
    """Hand one delta to the session output at a given time."""
    clock.now = at
    await output.capture_frame(delta(value))


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [True, False], ids=["live", "other-model"])
async def test_only_live_room_audio_output_is_lag_bounded(output_clock: OutputClock, live: bool) -> None:
    """GPT-Live speaks through the lag bound; other models keep RoomIO's output unchanged."""
    sink = PlayoutSink(output_clock)
    session = MagicMock()
    session.output.audio = sink
    audio, _room = await start_audio(live=live, session=session)
    await audio.aclose()
    output = session.output.audio
    assert (output is sink) is not live
    if live:
        assert output.sample_rate == 24000
        assert output.can_pause
        await deliver(output, output_clock, 7, at=0.0)
        output.flush()
        assert (await asyncio.wait_for(output.wait_for_playout(), timeout=1)).interrupted is False
        assert sink.values == [7]


@pytest.mark.asyncio
async def test_live_output_forwards_real_time_audio_with_jitter_and_gc_stalls(output_clock: OutputClock) -> None:
    """Normal provider lead, delivery jitter, and a production-sized GC stall drop nothing."""
    sink = PlayoutSink(output_clock)
    output = _bound_live_output_lag(sink)
    # GPT-Live arrives up to one 100 ms delta early, sometimes a little late.
    jitter = [0.0, -0.1, 0.0, 0.05, -0.05, -0.1, 0.02]
    with capture_logs() as logs:
        for value in range(20):
            await deliver(output, output_clock, value, at=value * 0.1 + jitter[value % len(jitter)])
        # A 0.37 s gen-2 GC pause delivers the deltas it held back together.
        for value in range(20, 24):
            await deliver(output, output_clock, value, at=2.37)
        for value in range(24, 60):
            await deliver(output, output_clock, value, at=value * 0.1 + jitter[value % len(jitter)])
        output.flush()
    assert sink.values == list(range(60))
    assert not [log for log in logs if log["event"] == "call_output_backlog_dropped"]


@pytest.mark.asyncio
async def test_live_output_drops_audio_a_stall_queued_and_resyncs(output_clock: OutputClock) -> None:
    """Audio held back by a 1.5 s stall cannot play late for the rest of the utterance."""
    sink = PlayoutSink(output_clock)
    output = _bound_live_output_lag(sink)
    with capture_logs() as logs:
        for value in range(10):
            await deliver(output, output_clock, value, at=value * 0.1)
        # The loop stalls from 1.0 s to 2.5 s; the provider kept streaming in real time.
        for value in range(10, 25):
            await deliver(output, output_clock, value, at=2.5)
        for value in range(25, 45):
            await deliver(output, output_clock, value, at=value * 0.1 + 0.04)
        output.flush()

    queued = sink.queued_at_delivery()
    # No delivered delta waits behind more than the bound, so after a barge-in at
    # most that much already-generated speech can still play.
    assert max(queued) <= 0.5 + 1e-9
    assert sink.values[:10] == list(range(10))
    dropped = sorted(set(range(45)) - set(sink.values))
    # One contiguous stale stretch goes: the 1.5 s stall minus the normal 100 ms lead.
    assert dropped == list(range(dropped[0], dropped[-1] + 1))
    assert len(dropped) == 14
    # Afterwards every delta plays, back at the provider's normal 100 ms lead.
    assert sink.values[-10:] == list(range(35, 45))
    assert queued[-1] <= 0.1 + 1e-9
    drop_logs = [log for log in logs if log["event"] == "call_output_backlog_dropped"]
    assert drop_logs == [
        {"event": "call_output_backlog_dropped", "log_level": "warning", "dropped_ms": len(dropped) * 100},
    ]


@pytest.mark.asyncio
async def test_live_output_preserves_segment_playout_and_controls(output_clock: OutputClock) -> None:
    """Every segment still reports playout, and flush, clear, pause, and resume reach RoomIO."""
    sink = PlayoutSink(output_clock)
    output = _bound_live_output_lag(sink)
    started: list[float] = []
    output.on("playback_started", lambda event: started.append(event.created_at))
    with capture_logs() as logs:
        # A whole stalled segment arrives at once and ends while audio is still being dropped.
        for value in range(10):
            await deliver(output, output_clock, value, at=1.0)
        output.flush()
        await asyncio.wait_for(output.wait_for_playout(), timeout=1)
        dropped_first = 10 - len(sink.values)
        assert dropped_first > 0
        # The next segment opens behind that queue; its first delta still plays so
        # RoomIO reports the segment, and later stale deltas are dropped.
        for value in range(10, 13):
            await deliver(output, output_clock, value, at=1.0)
        assert 10 in sink.values
        assert 11 not in sink.values
        # An interruption flushes and then clears, as LiveKit's forwarding does.
        output.flush()
        output.clear_buffer()
        await asyncio.wait_for(output.wait_for_playout(), timeout=1)
        # Clearing discarded RoomIO's queue, so new audio is not stale.
        for value in range(13, 16):
            await deliver(output, output_clock, value, at=1.0)
        output.flush()
        await asyncio.wait_for(output.wait_for_playout(), timeout=1)
    output.pause()
    output.resume()

    assert sink.values[-3:] == [13, 14, 15]
    assert sink.calls == ["flush", "flush", "clear", "flush", "pause", "resume"]
    assert started == [1.0, 1.0, 1.0]
    assert output.captured_playout_segments == 3
    assert [log["dropped_ms"] for log in logs if log["event"] == "call_output_backlog_dropped"] == [
        dropped_first * 100,
        200,
    ]
