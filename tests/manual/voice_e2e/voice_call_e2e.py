#!/usr/bin/env python3
"""Scripted end-to-end voice call against a running voice-test MindRoom instance.

A synthetic caller joins a MatrixRTC call with the agent on the local stack
(`compose.yaml`), speaks OpenAI TTS utterances into a paced microphone track,
and records everything it hears with wall-clock timestamps:

- `mic.wav` / `mic.jsonl`: every 20 ms frame the caller sent (`t_ns`, `voiced`).
- `rx.wav` / `rx.jsonl`: every frame received from the agent (`t_ns`, `rms`).
- `stats.jsonl`: WebRTC inbound-rtp stats of the agent track, once per second.
- `markers.jsonl`: scenario steps (utterance start/end, waits, barge-in).

Run `analyze.py <run-dir>` afterwards for latency, barge-in and gap metrics.

Usage:
    .venv314/bin/python tests/manual/voice_e2e/voice_call_e2e.py baseline
"""

from __future__ import annotations

import argparse
import array
import asyncio
import hashlib
import io
import json
import math
import os
import re
import secrets
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from analyze import delegation_answered
from google.protobuf.json_format import MessageToDict
from livekit import rtc

from mindroom.matrix_rtc.events import (
    CALL_MEMBER_EVENT_TYPE,
    build_membership_content,
    membership_state_key,
)
from mindroom.matrix_rtc.focus import OpenIDToken, request_sfu_grant

ROOT = Path(os.environ.get("MINDROOM_VOICETEST_ROOT", Path.home() / ".mindroom-voicetest"))
INSTANCE_LOG = ROOT / "run" / "instance.log"
HOMESERVER = "http://localhost:8008"
SERVICE_URL = "http://127.0.0.1:8781"
AGENT_NAME = "openclaw"
AGENT_USER_ID = f"@mindroom_{AGENT_NAME}:localhost"
CALLER_LOCALPART = "voicecaller"

SAMPLE_RATE = 24000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
VOICED_RMS = 200.0
MEMBERSHIP_EXPIRES_MS = 30 * 60 * 1000


def log(message: str) -> None:
    """Print one timestamped progress line."""
    sys.stdout.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    sys.stdout.flush()


def rms(pcm: bytes) -> float:
    """Return the RMS of little-endian PCM16 samples."""
    samples = array.array("h", pcm)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def silence(ms: int) -> bytes:
    """Return `ms` milliseconds of PCM16 silence."""
    return b"\x00\x00" * (SAMPLE_RATE * ms // 1000)


async def tts_pcm(text: str, openai_key: str) -> bytes:
    """Synthesize `text` to 24 kHz mono PCM16, cached on disk."""
    cache = ROOT / "tts-cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (hashlib.sha256(text.encode()).hexdigest()[:24] + ".pcm")
    if path.exists():
        return path.read_bytes()
    async with httpx.AsyncClient(timeout=120.0) as http:
        response = await http.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {openai_key}"},
            json={"model": "tts-1", "voice": "alloy", "input": text, "response_format": "wav"},
        )
        response.raise_for_status()
    with wave.open(io.BytesIO(response.content)) as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
            msg = f"unexpected TTS format {wav.getparams()}"
            raise RuntimeError(msg)
        pcm = wav.readframes(wav.getnframes())
    path.write_bytes(pcm)
    return pcm


async def utterance_pcm(parts: list[str | int], openai_key: str) -> bytes:
    """Concatenate TTS phrases and silences (ints are milliseconds of silence)."""
    chunks = [silence(part) if isinstance(part, int) else await tts_pcm(part, openai_key) for part in parts]
    return b"".join(chunks)


@dataclass
class Recorder:
    """In-memory timestamped audio and event log, written once at the end."""

    out: Path
    mic_pcm: bytearray = field(default_factory=bytearray)
    mic_rows: list[dict] = field(default_factory=list)
    rx_pcm: bytearray = field(default_factory=bytearray)
    rx_rows: list[dict] = field(default_factory=list)
    stats_rows: list[dict] = field(default_factory=list)
    markers: list[dict] = field(default_factory=list)

    def mark(self, step: str, **fields: object) -> None:
        """Record one scenario marker."""
        self.markers.append({"t_ns": time.time_ns(), "step": step, **fields})
        log(f"  [{step}] {json.dumps(fields) if fields else ''}")

    def write(self) -> None:
        """Flush recordings to the run directory."""
        self.out.mkdir(parents=True, exist_ok=True)
        for name, pcm in (("mic", self.mic_pcm), ("rx", self.rx_pcm)):
            with wave.open(str(self.out / f"{name}.wav"), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(bytes(pcm))
        for name, rows in (
            ("mic", self.mic_rows),
            ("rx", self.rx_rows),
            ("stats", self.stats_rows),
            ("markers", self.markers),
        ):
            (self.out / f"{name}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class Mic:
    """Deadline-paced 20 ms microphone pump with a small native queue."""

    def __init__(self, recorder: Recorder) -> None:
        self.source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=60)
        self._recorder = recorder
        self._speech = bytearray()
        self._done = asyncio.Event()
        self._done.set()

    def say(self, pcm: bytes) -> None:
        """Queue speech; `wait_done()` resolves after the last frame is sent."""
        self._speech.extend(pcm)
        self._done.clear()

    async def wait_done(self) -> None:
        """Wait until queued speech has been sent."""
        await self._done.wait()

    async def pump(self) -> None:
        """Send one frame per 20 ms deadline: queued speech or silence."""
        frame_bytes = FRAME_SAMPLES * 2
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            if self._speech:
                chunk = bytes(self._speech[:frame_bytes]).ljust(frame_bytes, b"\x00")
                del self._speech[:frame_bytes]
                if not self._speech:
                    self._done.set()
            else:
                chunk = silence(FRAME_MS)
            level = rms(chunk)
            self._recorder.mic_pcm.extend(chunk)
            self._recorder.mic_rows.append({"t_ns": time.time_ns(), "voiced": level > VOICED_RMS, "rms": round(level)})
            await self.source.capture_frame(rtc.AudioFrame(chunk, SAMPLE_RATE, 1, FRAME_SAMPLES))
            deadline += FRAME_MS / 1000
            delay = deadline - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                deadline = loop.time()


class Listener:
    """Consumes the agent's audio track and tracks voiced activity live."""

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder
        self.track: rtc.RemoteAudioTrack | None = None
        self.voiced_ms = 0.0
        self.last_voiced_ns = 0

    def quiet_for(self) -> float:
        """Seconds since the agent was last voiced (inf if never)."""
        if not self.last_voiced_ns:
            return math.inf
        return (time.time_ns() - self.last_voiced_ns) / 1e9

    async def consume(self, track: rtc.RemoteAudioTrack) -> None:
        """Record every received frame with its arrival time."""
        self.track = track
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        try:
            async for event in stream:
                pcm = bytes(event.frame.data)
                now = time.time_ns()
                level = rms(pcm)
                self._recorder.rx_pcm.extend(pcm)
                self._recorder.rx_rows.append(
                    {"t_ns": now, "samples": event.frame.samples_per_channel, "rms": round(level)},
                )
                if level > VOICED_RMS:
                    self.voiced_ms += event.frame.samples_per_channel * 1000 / SAMPLE_RATE
                    self.last_voiced_ns = now
        finally:
            await stream.aclose()

    async def poll_stats(self) -> None:
        """Sample inbound-rtp stats of the agent track once per second."""
        while True:
            await asyncio.sleep(1.0)
            if self.track is None:
                continue
            stats = await self.track.get_stats()
            rows = [MessageToDict(stat) for stat in stats]
            inbound = [row for row in rows if "inboundRtp" in row]
            self._recorder.stats_rows.append({"t_ns": time.time_ns(), "inbound": inbound})


@dataclass
class Caller:
    """Caller account plus live call resources."""

    user_id: str
    device_id: str
    token: str = field(repr=False)
    room: rtc.Room | None = None
    mic: Mic | None = None
    listener: Listener | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)

    @property
    def headers(self) -> dict[str, str]:
        """Return Matrix auth headers."""
        return {"Authorization": f"Bearer {self.token}"}


async def load_caller(http: httpx.AsyncClient) -> Caller:
    """Reuse the saved caller session, registering the caller only when it is missing or invalid."""
    secret_path = ROOT / "caller.json"
    if secret_path.exists():
        saved = json.loads(secret_path.read_text(encoding="utf-8"))
        whoami = await http.get(
            "/_matrix/client/v3/account/whoami",
            headers={"Authorization": f"Bearer {saved['access_token']}"},
        )
        if whoami.status_code == 200:
            return Caller(user_id=saved["user_id"], device_id=saved["device_id"], token=saved["access_token"])
    response = await http.post(
        "/_matrix/client/v3/register",
        json={"username": CALLER_LOCALPART, "password": secrets.token_hex(16), "auth": {"type": "m.login.dummy"}},
    )
    response.raise_for_status()
    body = response.json()
    session = {key: body[key] for key in ("user_id", "device_id", "access_token")}
    secret_path.write_text(json.dumps(session), encoding="utf-8")
    secret_path.chmod(0o600)
    return Caller(user_id=body["user_id"], device_id=body["device_id"], token=body["access_token"])


async def create_call_room(http: httpx.AsyncClient, caller: Caller, name: str) -> str:
    """Create a private room with the agent and wait until it has joined."""
    response = await http.post(
        "/_matrix/client/v3/createRoom",
        headers=caller.headers,
        json={
            "name": name,
            "invite": [AGENT_USER_ID],
            "preset": "private_chat",
            "power_level_content_override": {"events": {CALL_MEMBER_EVENT_TYPE: 0}},
        },
    )
    response.raise_for_status()
    room_id = response.json()["room_id"]
    for _ in range(120):
        members = await http.get(f"/_matrix/client/v3/rooms/{room_id}/joined_members", headers=caller.headers)
        if AGENT_USER_ID in members.json().get("joined", {}):
            return room_id
        await asyncio.sleep(0.5)
    msg = f"agent did not join {room_id}"
    raise TimeoutError(msg)


async def start_call(http: httpx.AsyncClient, caller: Caller, room_id: str, recorder: Recorder) -> None:
    """Publish call membership, join the SFU, and start mic/listener tasks."""
    content = build_membership_content(
        user_id=caller.user_id,
        device_id=caller.device_id,
        livekit_service_url=SERVICE_URL,
        expires_ms=MEMBERSHIP_EXPIRES_MS,
    )
    state_key = membership_state_key(caller.user_id, caller.device_id)
    response = await http.put(
        f"/_matrix/client/v3/rooms/{room_id}/state/{CALL_MEMBER_EVENT_TYPE}/{state_key}",
        headers=caller.headers,
        json=content,
    )
    response.raise_for_status()
    openid = (
        await http.post(
            f"/_matrix/client/v3/user/{caller.user_id}/openid/request_token",
            headers=caller.headers,
            json={},
        )
    ).json()
    grant = await request_sfu_grant(
        SERVICE_URL,
        room_id=room_id,
        device_id=caller.device_id,
        openid_token=OpenIDToken(
            openid["access_token"],
            openid["expires_in"],
            openid["matrix_server_name"],
            openid["token_type"],
        ),
        allow_private_networks=True,
    )
    room = rtc.Room()
    listener = Listener(recorder)
    mic = Mic(recorder)

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, _publication: object, participant: rtc.RemoteParticipant) -> None:
        if isinstance(track, rtc.RemoteAudioTrack):
            recorder.mark("agent_track_subscribed", identity=participant.identity)
            caller.tasks.append(asyncio.create_task(listener.consume(track)))

    await room.connect(grant.url, grant.jwt)
    track = rtc.LocalAudioTrack.create_audio_track("mic", mic.source)
    await room.local_participant.publish_track(
        track,
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )
    caller.room, caller.mic, caller.listener = room, mic, listener
    caller.tasks += [asyncio.create_task(mic.pump()), asyncio.create_task(listener.poll_stats())]
    recorder.mark("caller_joined_sfu", identity=room.local_participant.identity)


async def end_call(http: httpx.AsyncClient, caller: Caller, room_id: str) -> None:
    """Leave the SFU and clear the call membership."""
    for task in caller.tasks:
        task.cancel()
    await asyncio.gather(*caller.tasks, return_exceptions=True)
    if caller.room is not None:
        await caller.room.disconnect()
    if caller.mic is not None:
        await caller.mic.source.aclose()
    state_key = membership_state_key(caller.user_id, caller.device_id)
    await http.put(
        f"/_matrix/client/v3/rooms/{room_id}/state/{CALL_MEMBER_EVENT_TYPE}/{state_key}",
        headers=caller.headers,
        json={},
    )


def transcript_text(started_after: float) -> str:
    """Return the newest call transcript written after `started_after`."""
    calls = ROOT / "mindroom_data" / "agents" / AGENT_NAME / "workspace" / "calls"
    files = [path for path in calls.glob("*.md") if path.stat().st_mtime >= started_after] if calls.exists() else []
    return max(files, key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8") if files else ""


def delegation_records(offset: int) -> list[dict]:
    """Delegation events among the complete instance log lines after byte `offset`."""
    with INSTANCE_LOG.open("rb") as log_file:
        log_file.seek(offset)
        data = log_file.read()
    lines = data[: data.rfind(b"\n") + 1].decode("utf-8", errors="replace").splitlines()
    return [json.loads(line) for line in lines if line.startswith("{") and '"delegation_id"' in line]


class Scenario:
    """Step helpers shared by scenario scripts."""

    def __init__(self, caller: Caller, recorder: Recorder, openai_key: str, started: float) -> None:
        self.caller = caller
        self.recorder = recorder
        self.openai_key = openai_key
        self.started = started
        self.log_offset = 0

    @property
    def listener(self) -> Listener:
        """Return the live listener."""
        assert self.caller.listener is not None
        return self.caller.listener

    async def say(self, label: str, parts: list[str | int]) -> None:
        """Speak an utterance and mark when it starts and finishes."""
        assert self.caller.mic is not None
        pcm = await utterance_pcm(parts, self.openai_key)
        self.log_offset = INSTANCE_LOG.stat().st_size
        self.recorder.mark("say_start", label=label, audio_ms=len(pcm) // 2 * 1000 // SAMPLE_RATE)
        self.caller.mic.say(pcm)
        await self.caller.mic.wait_done()
        self.recorder.mark("say_end", label=label)

    async def wait_speaking(self, label: str, timeout_s: float = 60.0) -> bool:
        """Wait until the agent produces voiced audio after this call."""
        start_ns = time.time_ns()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.listener.last_voiced_ns > start_ns:
                self.recorder.mark("agent_speaking", label=label)
                return True
            await asyncio.sleep(0.02)
        self.recorder.mark("timeout_speaking", label=label)
        return False

    async def wait_quiet(self, label: str, quiet_s: float = 2.0, timeout_s: float = 120.0) -> bool:
        """Wait until the agent has been silent for `quiet_s` seconds."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.listener.quiet_for() >= quiet_s:
                self.recorder.mark("agent_quiet", label=label, quiet_s=quiet_s)
                return True
            await asyncio.sleep(0.05)
        self.recorder.mark("timeout_quiet", label=label)
        return False

    async def wait_transcript(self, label: str, pattern: str, timeout_s: float = 90.0) -> bool:
        """Wait until the call transcript matches `pattern` (case-insensitive)."""
        regex = re.compile(pattern, re.IGNORECASE)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if regex.search(transcript_text(self.started)):
                self.recorder.mark("transcript_match", label=label, pattern=pattern)
                return True
            await asyncio.sleep(0.25)
        self.recorder.mark("timeout_transcript", label=label, pattern=pattern)
        return False

    async def wait_delegated(self, label: str, timeout_s: float = 90.0) -> bool:
        """Wait until the delegations since the last utterance finished and the agent spoke after them."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if delegation_answered(delegation_records(self.log_offset), self.listener.last_voiced_ns / 1e9):
                self.recorder.mark("delegation_answered", label=label)
                return True
            await asyncio.sleep(0.25)
        self.recorder.mark("timeout_delegation", label=label)
        return False

    async def reply(
        self,
        label: str,
        pattern: str | None = None,
        timeout_s: float = 90.0,
        delegated: bool = False,
    ) -> None:
        """Wait for a complete agent reply, optionally one matching `pattern` or answering a delegation."""
        await self.wait_speaking(label, timeout_s=timeout_s)
        if pattern is not None:
            await self.wait_transcript(label, pattern, timeout_s=timeout_s)
        if delegated:
            await self.wait_delegated(label, timeout_s=timeout_s)
        await self.wait_quiet(label)

    async def wait_voiced(self, label: str, seconds: float, timeout_s: float = 120.0) -> bool:
        """Wait until the agent has produced `seconds` more voiced audio."""
        base = self.listener.voiced_ms
        deadline = time.monotonic() + timeout_s
        while self.listener.voiced_ms - base < seconds * 1000:
            if time.monotonic() > deadline:
                self.recorder.mark("timeout_voiced", label=label, seconds=seconds)
                return False
            await asyncio.sleep(0.02)
        self.recorder.mark("agent_voiced", label=label, seconds=seconds)
        return True

    async def barge_in(self, label: str, parts: list[str | int], after_speech_s: float) -> None:
        """Interrupt once the agent has spoken `after_speech_s` seconds of this reply."""
        await self.wait_speaking(label)
        await self.wait_voiced(label, after_speech_s)
        await self.say(label, parts)

    def inject_stall(self, seconds: float) -> None:
        """Block the instance's event-loop thread for `seconds` via sys.remote_exec."""
        pid = int((ROOT / "run" / "instance.pid").read_text())
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=ROOT / "run") as script:
            script.write(f"import time\ntime.sleep({seconds})\n")
        sys.remote_exec(pid, script.name)
        self.recorder.mark("stall_injected", seconds=seconds)


async def scenario_baseline(s: Scenario) -> None:
    """Greeting, direct answer, delegated tool answer, fragmented question, barge-in."""
    await s.reply("greeting", timeout_s=45)
    await s.say("direct", ["Quick one. What is the capital of France?"])
    await s.reply("direct", pattern=r"paris")
    await s.say(
        "delegated_tool",
        ["Please use your calculator tool to multiply three thousand four hundred eighty seven by twenty nine."],
    )
    await s.reply("delegated_tool", pattern=r"101[, ]?123|one hundred (and )?one thousand")
    await s.say(
        "fragmented",
        [
            "So I was thinking about the weekend,",
            900,
            "and I wonder whether",
            800,
            "we should leave on Friday",
            1000,
            "or rather on Saturday morning. What do you think?",
        ],
    )
    await s.reply("fragmented")
    await s.say("long_story", ["Tell me a long and detailed story about a lighthouse keeper. Take at least a minute."])
    await s.barge_in("barge_in", ["Stop. Stop please. Wait."], after_speech_s=6.0)
    await s.wait_quiet("after_barge_in", quiet_s=3.0, timeout_s=60)
    await s.say("closing", ["Okay, never mind the story. Thanks, bye."])
    await s.reply("closing", timeout_s=30)


async def scenario_stall(s: Scenario) -> None:
    """Inject 0.3 s, 0.8 s and 1.5 s event-loop stalls while the agent speaks."""
    await s.reply("greeting", timeout_s=45)
    await s.say(
        "long_story",
        ["Tell me a long and detailed story about a lighthouse keeper. Take at least two minutes."],
    )
    await s.wait_speaking("long_story")
    for seconds in (0.3, 0.8, 1.5):
        await s.wait_voiced(f"before_stall_{seconds}", 4.0)
        s.inject_stall(seconds)
    await s.wait_voiced("after_stalls", 4.0)
    await s.say("barge_in", ["Stop. Stop please. Wait."])
    await s.wait_quiet("after_barge_in", quiet_s=3.0, timeout_s=60)
    await s.say("closing", ["Okay, thanks, bye."])
    await s.reply("closing", timeout_s=30)


async def scenario_delegated_barge(s: Scenario) -> None:
    """Interrupt a long delegated answer, then stay silent to catch stale speech."""
    await s.reply("greeting", timeout_s=45)
    await s.say(
        "delegated_long",
        [
            "Please use your web search tool to look up recent news about the James Webb Space Telescope,"
            " and give me a detailed rundown of the three most interesting findings.",
        ],
    )
    await s.wait_speaking("delegated_long")
    await s.wait_voiced("delegated_long_answer", 8.0, timeout_s=120)
    await s.say("barge_in_delegated", ["Stop. Stop. Wait a second."])
    await s.wait_quiet("after_barge_in", quiet_s=2.0, timeout_s=60)
    s.recorder.mark("silent_watch_start")
    await asyncio.sleep(25)
    s.recorder.mark("silent_watch_end", agent_voiced_ms=round(s.listener.voiced_ms))
    await s.say("closing", ["Okay, never mind. Thanks, bye."])
    await s.reply("closing", timeout_s=30)


async def scenario_queue(s: Scenario) -> None:
    """Two delegated requests back to back to expose delegation serialization."""
    await s.reply("greeting", timeout_s=45)
    await s.say("queue_first", ["Use your calculator tool to multiply forty eight by fifty two."])
    await asyncio.sleep(1.5)
    await s.say("queue_second", ["And also use your calculator tool to divide nine thousand by seven."])
    await s.wait_transcript("queue_first", r"2[, ]?496|two thousand four hundred ninety", timeout_s=90)
    await s.wait_transcript("queue_second", r"1[, ]?285|one thousand two hundred eighty", timeout_s=90)
    await s.wait_quiet("queue_done", quiet_s=2.5)
    await s.say("closing", ["Thanks, bye."])
    await s.reply("closing", timeout_s=30)


async def scenario_latency(s: Scenario) -> None:
    """Delegated questions (tool, web search, memory), two rounds, for delegate-model comparisons."""
    await s.reply("greeting", timeout_s=45)
    rounds = (
        ("forty three", "fifty seven", r"2[, ]?451|two thousand four hundred fifty"),
        ("sixty one", "eighty nine", r"5[, ]?429|five thousand four hundred twenty"),
    )
    for index, (a, b, pattern) in enumerate(rounds):
        await s.say(f"calc_{index}", [f"Use your calculator tool to multiply {a} by {b}."])
        await s.reply(f"calc_{index}", pattern=pattern)
        await asyncio.sleep(1.0)
    for index, topic in enumerate(("the most recent Tour de France winner", "the current price of Bitcoin")):
        await s.say(f"search_{index}", [f"Use your web search tool to look up {topic}. Answer in one sentence."])
        await s.reply(f"search_{index}", timeout_s=120, delegated=True)
    for index, question in enumerate(("what my favorite programming language is", "where I live")):
        await s.say(f"memory_{index}", [f"Check your memory notes for {question}. Answer in one sentence."])
        await s.reply(f"memory_{index}", timeout_s=120, delegated=True)
    await s.say("closing", ["Thanks, bye."])
    await s.reply("closing", timeout_s=30)


async def scenario_probe(s: Scenario) -> None:
    """One delegated memory question, for profiling a single delegate turn."""
    await s.reply("greeting", timeout_s=45)
    await s.say("memory_probe", ["Check your memory notes for what my favorite programming language is. One sentence."])
    await s.reply("memory_probe", timeout_s=120, delegated=True)
    await s.say("closing", ["Thanks, bye."])
    await s.reply("closing", timeout_s=30)


async def scenario_stale_queue(s: Scenario) -> None:
    """Queue two delegations, cancel both by voice, and watch for stale answers."""
    await s.reply("greeting", timeout_s=45)
    await s.say("stale_first", ["Use your web search tool to look up the history of the Eiffel Tower in detail."])
    await asyncio.sleep(1.0)
    await s.say("stale_second", ["And also use your calculator tool to multiply seven hundred seventy seven by three."])
    await asyncio.sleep(2.0)
    await s.say("barge_in_cancel", ["Stop. Never mind, forget both of those. I don't need them anymore."])
    await s.wait_quiet("after_cancel", quiet_s=2.0, timeout_s=60)
    s.recorder.mark("silent_watch_start")
    await asyncio.sleep(30)
    s.recorder.mark("silent_watch_end", agent_voiced_ms=round(s.listener.voiced_ms))
    await s.say("closing", ["Okay, bye."])
    await s.reply("closing", timeout_s=30)


SCENARIOS = {
    "stale_queue": scenario_stale_queue,
    "probe": scenario_probe,
    "latency": scenario_latency,
    "baseline": scenario_baseline,
    "delegated_barge": scenario_delegated_barge,
    "queue": scenario_queue,
    "stall": scenario_stall,
}


def copy_instance_artifacts(out: Path, started: float) -> None:
    """Copy the call transcript and the instance log window into the run dir."""
    text = transcript_text(started)
    if text:
        (out / "transcript.md").write_text(text, encoding="utf-8")
    if INSTANCE_LOG.exists():
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started - 5))
        rows = []
        for line in INSTANCE_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            try:
                stamp = json.loads(line).get("timestamp", "")
            except json.JSONDecodeError:
                continue
            if stamp >= start_iso:
                rows.append(line)
        (out / "instance.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")


async def main() -> int:
    """Run one scenario and write its recordings."""
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    openai_key = os.environ["OPENAI_API_KEY"]
    started = time.time()
    run_name = time.strftime("%Y%m%d-%H%M%S") + f"-{args.scenario}" + (f"-{args.tag}" if args.tag else "")
    out = ROOT / "runs" / run_name
    recorder = Recorder(out)
    log(f"run {out}")
    async with httpx.AsyncClient(base_url=HOMESERVER, timeout=30.0) as http:
        caller = await load_caller(http)
        room_id = await create_call_room(http, caller, f"voicetest {run_name}")
        recorder.mark("room_ready", room_id=room_id)
        try:
            await start_call(http, caller, room_id, recorder)
            await SCENARIOS[args.scenario](Scenario(caller, recorder, openai_key, started))
        finally:
            recorder.mark("hangup")
            await end_call(http, caller, room_id)
            recorder.write()
    await asyncio.sleep(3)  # let the agent finalize the transcript after the hangup
    copy_instance_artifacts(out, started)
    log(f"done {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
