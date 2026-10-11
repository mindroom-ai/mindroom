"""GPT-Live input stays paced across microphone gaps, and late output yields to the caller."""

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
from livekit.agents.voice.events import UserState, UserStateChangedEvent
from livekit.rtc._utils import RingQueue
from structlog.testing import capture_logs

import mindroom.matrix_rtc.voice_agent as voice_agent_module
from mindroom.matrix_rtc.voice_agent import (
    LiveVoiceAgentOptions,
    RealtimeVoiceBridge,
    VoiceAgentOptions,
    _AuthorizedParticipantAudioInput,
    _drop_live_backlog_on_caller_speech,
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

    def __init__(self) -> None:
        super().__init__(
            label="RoomIO",
            capabilities=agents_io.AudioOutputCapabilities(pause=True),
            sample_rate=24000,
        )
        self.values: list[int] = []
        self.calls: list[str] = []
        self.segment_open = False

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """Record one delivered delta by its sample value."""
        await super().capture_frame(frame)
        if not self.segment_open:
            self.segment_open = True
            self.on_playback_started(created_at=1.0)
        self.values.append(frame.data[0])

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


class CallSession:
    """An agent session stub whose RoomIO output is a recording sink and whose events are real."""

    def __init__(self) -> None:
        self.sink = PlayoutSink()
        self.session = MagicMock()
        self.session.output.audio = self.sink
        self.events: rtc.EventEmitter[str] = rtc.EventEmitter()
        self.session.on = self.events.on

    @property
    def output(self) -> agents_io.AudioOutput:
        """Return the audio output the session speaks through."""
        return self.session.output.audio

    def caller(self, state: UserState) -> None:
        """Emit the caller state change LiveKit reports when GPT-Live hears or stops hearing them."""
        old_state: UserState = "listening" if state == "speaking" else "speaking"
        self.events.emit("user_state_changed", UserStateChangedEvent(old_state=old_state, new_state=state))


@pytest.fixture
def call() -> CallSession:
    """Attach the GPT-Live backlog drop to a stub session."""
    call = CallSession()
    _drop_live_backlog_on_caller_speech(call.session)
    return call


def delta(value: int) -> rtc.AudioFrame:
    """Build one 100 ms GPT-Live output delta identified by its sample value."""
    return rtc.AudioFrame(value.to_bytes(2, "little", signed=True) * 2400, 24000, 1, 2400)


async def deliver(output: agents_io.AudioOutput, clock: OutputClock, value: int, *, at: float) -> None:
    """Hand one delta to the session output at a given time."""
    clock.now = at
    await output.capture_frame(delta(value))


def drop_logs(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the backlog drop warnings among captured logs."""
    return [log for log in logs if log["event"] == "call_output_backlog_dropped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [True, False], ids=["live", "other-model"])
async def test_only_live_calls_drop_backlog_when_the_caller_speaks(output_clock: OutputClock, live: bool) -> None:
    """GPT-Live speaks through the backlog drop; other models keep RoomIO's output unchanged."""
    call = CallSession()
    audio, _room = await start_audio(live=live, session=call.session)
    await audio.aclose()
    assert (call.output is call.sink) is not live
    for value in range(10):
        await deliver(call.output, output_clock, value, at=0.0)
    call.caller("speaking")
    assert ("clear" in call.sink.calls) is live


@pytest.mark.asyncio
async def test_live_output_plays_late_audio_until_the_caller_speaks(
    output_clock: OutputClock,
    call: CallSession,
) -> None:
    """Audio held back by a stall still plays late when the caller stays quiet."""
    with capture_logs() as logs:
        for value in range(10):
            await deliver(call.output, output_clock, value, at=value * 0.1)
        # The loop stalls from 1.0 s to 2.5 s; the provider kept streaming in real time.
        for value in range(10, 25):
            await deliver(call.output, output_clock, value, at=2.5)
        call.caller("listening")
        call.caller("away")
        for value in range(25, 45):
            await deliver(call.output, output_clock, value, at=value * 0.1 + 0.04)
        call.output.flush()
    assert call.sink.values == list(range(45))
    assert call.sink.calls == ["flush"]
    assert drop_logs(logs) == []


@pytest.mark.asyncio
async def test_caller_speech_over_late_audio_clears_it_once(output_clock: OutputClock, call: CallSession) -> None:
    """The caller talking over stall-delayed speech discards it, and the drop is logged once."""
    with capture_logs() as logs:
        for value in range(10):
            await deliver(call.output, output_clock, value, at=value * 0.1)
        for value in range(10, 25):
            await deliver(call.output, output_clock, value, at=2.5)
        # 1.5 s arrived at 2.5 s behind audio that finished at 1.0 s, so 1.3 s is still queued.
        output_clock.now = 2.7
        call.caller("speaking")
        call.caller("listening")
        call.caller("speaking")
        # The wrapper keeps forwarding; RoomIO decides what of the interrupted segment plays.
        await deliver(call.output, output_clock, 25, at=2.8)
    assert call.sink.values == list(range(26))
    assert call.sink.calls == ["clear"]
    assert drop_logs(logs) == [
        {"event": "call_output_backlog_dropped", "log_level": "warning", "dropped_ms": 1300},
    ]


@pytest.mark.asyncio
async def test_audio_discarded_after_a_clear_does_not_count_as_queued(
    output_clock: OutputClock,
    call: CallSession,
) -> None:
    """Frames RoomIO drops from a cleared segment cannot make a later short burst look late."""
    with capture_logs() as logs:
        for value in range(10):
            await deliver(call.output, output_clock, value, at=value * 0.1)
        for value in range(10, 16):
            await deliver(call.output, output_clock, value, at=2.5)
        call.caller("speaking")
        # The rest of the stalled burst still arrives, and RoomIO discards it.
        for value in range(16, 35):
            await deliver(call.output, output_clock, value, at=2.5)
        call.output.flush()
        call.caller("listening")
        await deliver(call.output, output_clock, 35, at=3.4)
        call.caller("speaking")
    assert call.sink.calls == ["clear", "flush"]
    assert len(drop_logs(logs)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("queued_deltas", "cleared"), [(1, False), (5, False), (6, True)])
async def test_caller_speech_leaves_up_to_half_a_second_queued(
    output_clock: OutputClock,
    call: CallSession,
    queued_deltas: int,
    cleared: bool,
) -> None:
    """The normal lead and short backlogs stay for GPT-Live to yield on its own."""
    with capture_logs() as logs:
        for value in range(queued_deltas):
            await deliver(call.output, output_clock, value, at=0.0)
        call.caller("speaking")
    assert (call.sink.calls == ["clear"]) is cleared
    assert len(drop_logs(logs)) == cleared


@pytest.mark.asyncio
async def test_live_output_passes_playout_and_controls_through(output_clock: OutputClock, call: CallSession) -> None:
    """Segments still report playout, and flush, clear, pause, and resume reach RoomIO."""
    output = call.output
    started: list[float] = []
    output.on("playback_started", lambda event: started.append(event.created_at))
    assert output.sample_rate == 24000
    assert output.can_pause
    for segment in range(2):
        await deliver(output, output_clock, segment, at=0.0)
        output.flush()
        playout = await asyncio.wait_for(output.wait_for_playout(), timeout=1)
        assert playout.interrupted is False
    output.clear_buffer()
    output.pause()
    output.resume()
    assert call.sink.values == [0, 1]
    assert call.sink.calls == ["flush", "flush", "clear", "pause", "resume"]
    assert started == [1.0, 1.0]
    assert output.captured_playout_segments == 2
