#!/usr/bin/env python3
"""Full live agent-speaking call test against prod mindroom.chat.

The complete voice-call loop with the REAL code path and a REAL OpenAI key:

  1. A synthetic caller registers, starts an Element Call in a fresh room,
     and connects to the deployed LiveKit SFU publishing a microphone track.
  2. The real CallManager (real nio client, real build_call_tools with a
     calculator toolkit, real gpt-realtime agent) joins the call.
  3. The caller hears the agent's spoken greeting (audio-energy check).
  4. The caller speaks a question (OpenAI TTS audio pushed into its mic
     track) asking the agent to multiply 15 by 23 with its calculator tool.
  5. The agent must call the `multiply` tool and speak the answer.
  6. On shutdown the transcript file and the agent's daily memory entry
     must exist and record the turns + tool use.

Run:
  MINDROOM_REG_TOKEN=... OPENAI_API_KEY=sk-... \
  SSL_CERT_FILE=$(python -c 'import certifi;print(certifi.where())') \
  .venv/bin/python tests/manual/scratch/live_agent_speaking_test.py
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import io
import os
import ssl
import sys
import tempfile
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import certifi
import httpx
import nio

SRC = str(Path(__file__).resolve().parents[3] / "src")
sys.path.insert(0, SRC)

from livekit import rtc  # noqa: E402

from mindroom.config.agent import AgentConfig  # noqa: E402
from mindroom.config.calls import CallsConfig  # noqa: E402
from mindroom.config.main import Config  # noqa: E402
from mindroom.config.memory import MemoryConfig  # noqa: E402
from mindroom.config.models import ModelConfig  # noqa: E402
from mindroom.constants import resolve_runtime_paths  # noqa: E402
from mindroom.matrix.state import MatrixState  # noqa: E402
from mindroom.matrix_rtc import call_manager as cm  # noqa: E402
from mindroom.matrix_rtc.events import (  # noqa: E402
    CALL_MEMBER_EVENT_TYPE,
    build_membership_content,
    membership_state_key,
)
from mindroom.matrix_rtc.focus import OpenIDToken, request_sfu_grant  # noqa: E402
from mindroom.tool_system.runtime_context import ToolRuntimeContext  # noqa: E402
from mindroom.tool_system.worker_routing import build_tool_execution_identity  # noqa: E402

if TYPE_CHECKING:
    from mindroom.message_target import MessageTarget
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

HOMESERVER = "https://mindroom.chat"
SERVICE_URL = "https://mindroom.chat/livekit/jwt"
REG_TOKEN = os.environ["MINDROOM_REG_TOKEN"].strip()
OPENAI_KEY = os.environ["OPENAI_API_KEY"].strip()
SUFFIX = uuid.uuid4().hex[:8]
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
AGENT = "assistant"

QUESTION = "Hi assistant! Please use your calculator tool to multiply fifteen by twenty three, and tell me the result."

SAMPLE_RATE = 24000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
VOICED_RMS = 250.0


def log(m: str) -> None:
    """Print one unbuffered progress line."""
    sys.stdout.write(m + "\n")
    sys.stdout.flush()


async def register(localpart: str) -> tuple[str, str, str]:
    """Token-register a throwaway user; returns (user_id, device_id, access_token)."""
    pw = uuid.uuid4().hex
    async with httpx.AsyncClient(base_url=HOMESERVER, timeout=30.0) as h:
        session_response = await h.post("/_matrix/client/v3/register", json={"username": localpart, "password": pw})
        session_body = session_response.json()
        session = session_body.get("session")
        if not isinstance(session, str):
            msg = f"registration did not start (status={session_response.status_code}, body={session_body})"
            raise TypeError(msg)
        r = await h.post(
            "/_matrix/client/v3/register",
            json={
                "username": localpart,
                "password": pw,
                "auth": {"type": "m.login.registration_token", "token": REG_TOKEN, "session": session},
            },
        )
    b = r.json()
    if not all(isinstance(b.get(field), str) for field in ("user_id", "device_id", "access_token")):
        msg = f"registration did not complete (status={r.status_code}, body={b})"
        raise TypeError(msg)
    return b["user_id"], b["device_id"], b["access_token"]


async def tts_pcm(text: str) -> bytes:
    """Synthesize the caller's question to 24kHz mono PCM16."""
    async with httpx.AsyncClient(timeout=120.0) as h:
        r = await h.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            json={"model": "gpt-4o-mini-tts", "voice": "alloy", "input": text, "response_format": "wav"},
        )
        r.raise_for_status()
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getnchannels() == 1, w.getnchannels()
        assert w.getsampwidth() == 2
        assert w.getframerate() == SAMPLE_RATE, w.getframerate()
        return w.readframes(w.getnframes())


class CallerAudio:
    """Paced mic pump: silence by default, queued speech when available."""

    def __init__(self) -> None:
        self.source = rtc.AudioSource(SAMPLE_RATE, 1)
        self._speech = bytearray()
        self._silence = b"\x00\x00" * FRAME_SAMPLES

    def say(self, pcm: bytes) -> None:
        """Queue PCM16 speech to be spoken into the mic track."""
        self._speech.extend(pcm)

    @property
    def speaking(self) -> bool:
        """Whether queued speech is still being played out."""
        return bool(self._speech)

    async def pump(self) -> None:
        """Feed 20ms frames into the audio source forever (paced by LiveKit)."""
        frame_bytes = FRAME_SAMPLES * 2
        while True:
            if self._speech:
                chunk = bytes(self._speech[:frame_bytes])
                del self._speech[:frame_bytes]
                if len(chunk) < frame_bytes:
                    chunk += b"\x00" * (frame_bytes - len(chunk))
            else:
                chunk = self._silence
            frame = rtc.AudioFrame(chunk, SAMPLE_RATE, 1, FRAME_SAMPLES)
            await self.source.capture_frame(frame)


class BotAudioMeter:
    """Counts voiced frames coming back from the bot's published track."""

    def __init__(self) -> None:
        self.frames = 0
        self.voiced = 0
        self.peak_rms = 0.0

    async def consume(self, track: rtc.Track) -> None:
        """Read the bot's audio stream and tally voiced frames by RMS."""
        stream = rtc.AudioStream(track)
        async for event in stream:
            samples = array.array("h", bytes(event.frame.data))
            if not samples:
                continue
            rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
            self.frames += 1
            self.peak_rms = max(self.peak_rms, rms)
            if rms > VOICED_RMS:
                self.voiced += 1


async def caller_start_call(
    user_id: str,
    device_id: str,
    token: str,
    room_id: str,
) -> tuple[rtc.Room, CallerAudio, BotAudioMeter, str]:
    """Publish the caller's call membership, connect to the SFU, publish a mic."""
    async with httpx.AsyncClient(base_url=HOMESERVER, timeout=30.0) as h:
        content = build_membership_content(
            user_id=user_id,
            device_id=device_id,
            livekit_service_url=SERVICE_URL,
            expires_ms=4 * 60 * 60 * 1000,
        )
        put = await h.put(
            f"/_matrix/client/v3/rooms/{room_id}/state/{CALL_MEMBER_EVENT_TYPE}/{membership_state_key(user_id, device_id)}",
            params={"access_token": token},
            json=content,
        )
        put.raise_for_status()
        openid_response = await h.post(
            f"/_matrix/client/v3/user/{user_id}/openid/request_token",
            params={"access_token": token},
            json={},
        )
        openid_response.raise_for_status()
        oid = openid_response.json()
    grant = await request_sfu_grant(
        SERVICE_URL,
        room_id=room_id,
        device_id=device_id,
        openid_token=OpenIDToken(oid["access_token"], oid["expires_in"], oid["matrix_server_name"], oid["token_type"]),
    )
    room = rtc.Room()
    meter = BotAudioMeter()
    consumers: set[asyncio.Task[None]] = set()

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, _pub: object, participant: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            log(f"  [caller] subscribed to audio from {participant.identity}")
            task = asyncio.create_task(meter.consume(track))
            consumers.add(task)
            task.add_done_callback(consumers.discard)

    await room.connect(grant.url, grant.jwt)
    audio = CallerAudio()
    mic = rtc.LocalAudioTrack.create_audio_track("mic", audio.source)
    await room.local_participant.publish_track(mic, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    log(f"  [caller] on SFU as {room.local_participant.identity}, mic published")
    return room, audio, meter, grant.url


async def wait_for(predicate, timeout_s: float, what: str) -> bool:  # noqa: ANN001
    """Poll a predicate every 0.5s until true or the deadline passes."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.5)
    log(f"  TIMEOUT waiting for {what}")
    return False


async def main() -> int:  # noqa: PLR0915
    """Run the full agent-speaking call loop and report PASS/FAIL per leg."""
    log("== TTS: synthesizing the caller's question ==")
    question_pcm = await tts_pcm(QUESTION)
    log(f"  {len(question_pcm) // 2 / SAMPLE_RATE:.1f}s of question audio ready")

    log("== register caller + bot ==")
    caller_id, caller_dev, caller_tok = await register(f"speak_caller_{SUFFIX}")
    bot_id, bot_dev, bot_tok = await register(f"speak_bot_{SUFFIX}")
    log(f"  caller={caller_id} bot={bot_id}")

    log("== create room, bot joins ==")
    async with httpx.AsyncClient(base_url=HOMESERVER, timeout=30.0) as h:
        create_response = await h.post(
            "/_matrix/client/v3/createRoom",
            params={"access_token": caller_tok},
            json={
                "name": f"speaktest-{SUFFIX}",
                "invite": [bot_id],
                "preset": "public_chat",
                "power_level_content_override": {"events": {CALL_MEMBER_EVENT_TYPE: 0}},
            },
        )
        create_response.raise_for_status()
        room_id = create_response.json()["room_id"]
        join_response = await h.post(
            f"/_matrix/client/v3/rooms/{room_id}/join",
            params={"access_token": bot_tok},
            json={},
        )
        join_response.raise_for_status()
    log(f"  room={room_id}")

    log("== caller starts the call (mic publishing silence) ==")
    caller_room, caller_audio, meter, _sfu = await caller_start_call(caller_id, caller_dev, caller_tok, room_id)
    pump_task = asyncio.create_task(caller_audio.pump())

    log("== real CallManager joins with real key + real calculator tool ==")
    storage = Path(tempfile.mkdtemp(prefix="speaktest_"))
    config_path = storage / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=storage / "mindroom_data",
        process_env={"OPENAI_API_KEY": OPENAI_KEY, "MATRIX_HOMESERVER": HOMESERVER, "MINDROOM_NAMESPACE": ""},
    )
    # Entity resolution (used by build_call_tools -> create_agent) needs the
    # managed accounts for the router and this agent persisted in matrix state.
    state = MatrixState.load(paths)
    state.add_account("agent_router", f"speak_router_{SUFFIX}", "unused", domain="mindroom.chat")
    state.add_account(
        "agent_assistant",
        bot_id.split(":")[0].lstrip("@"),
        "unused",
        domain="mindroom.chat",
        device_id=bot_dev,
        access_token=bot_tok,
    )
    state.save(paths)

    config = Config(
        agents={AGENT: AgentConfig(display_name="Assistant", role="Helpful voice assistant", tools=["calculator"])},
        models={"default": ModelConfig(provider="openai", id="gpt-5.5")},
        memory=MemoryConfig(backend="none"),
        calls=CallsConfig(enabled=True, agents=[AGENT], livekit_service_url=SERVICE_URL),
    )

    bot_client = nio.AsyncClient(HOMESERVER, bot_id, device_id=bot_dev, ssl=SSL_CTX)
    bot_client.access_token = bot_tok
    bot_client.user_id = bot_id
    bot_client.device_id = bot_dev

    class LiveToolSupport:
        """Minimal stand-in for the bot-runtime ToolRuntimeSupport."""

        def build_context(
            self,
            target: MessageTarget,
            *,
            user_id: str | None,
            **_kw: object,
        ) -> ToolRuntimeContext:
            return ToolRuntimeContext(
                agent_name=AGENT,
                target=target,
                requester_id=user_id or bot_id,
                client=bot_client,
                config=config,
                runtime_paths=paths,
                event_cache=SimpleNamespace(),
                conversation_cache=SimpleNamespace(),
                storage_path=paths.storage_root,
            )

        def build_execution_identity(
            self,
            *,
            target: MessageTarget,
            user_id: str | None,
            agent_name: str | None = None,
        ) -> ToolExecutionIdentity:
            return build_tool_execution_identity(
                channel="matrix",
                agent_name=agent_name or AGENT,
                transport_agent_name=AGENT,
                runtime_paths=paths,
                requester_id=user_id or bot_id,
                room_id=target.room_id,
                thread_id=target.source_thread_id,
                resolved_thread_id=target.resolved_thread_id,
                session_id=target.session_id,
            )

    manager = cm.CallManager(
        agent_name=AGENT,
        config=config,
        client=bot_client,
        runtime_paths=paths,
        ssl_verify=True,
        tool_support=LiveToolSupport(),  # type: ignore[arg-type]
    )

    room_obj = nio.MatrixRoom(room_id=room_id, own_user_id=bot_id)
    room_obj.encrypted = False
    event = nio.UnknownEvent({"event_id": "$evt", "sender": caller_id, "origin_server_ts": 1}, CALL_MEMBER_EVENT_TYPE)
    manager_shutdown = False
    try:
        await asyncio.wait_for(manager.on_room_event(room_obj, event), timeout=120)
        log("  [bot] call join path completed (agent session started)")

        results: dict[str, bool] = {}

        log("== leg 1: greeting audio from the agent ==")
        results["greeting_audio"] = await wait_for(lambda: meter.voiced >= 20, 45, "greeting audio")
        log(f"  bot audio: frames={meter.frames} voiced={meter.voiced} peak_rms={meter.peak_rms:.0f}")

        transcript_dir = paths.storage_root / "calls" / AGENT

        def transcript_text() -> str:
            files = list(transcript_dir.glob("*.md")) if transcript_dir.exists() else []
            return files[0].read_text(encoding="utf-8") if files else ""

        log("== leg 2: caller asks the calculator question ==")
        voiced_before_answer = meter.voiced
        caller_audio.say(question_pcm)
        await wait_for(lambda: not caller_audio.speaking, 30, "question audio to finish playing")
        log("  question spoken; waiting for tool call + spoken answer")

        def answer_present() -> bool:
            text = transcript_text().lower().replace("-", " ")
            return "345" in text or ("three hundred" in text and "forty five" in text)

        results["tool_called"] = await wait_for(
            lambda: "tools used" in transcript_text() and "multiply" in transcript_text(),
            75,
            "tool use in transcript",
        )
        results["answer_spoken"] = await wait_for(lambda: meter.voiced > voiced_before_answer + 20, 30, "answer audio")
        results["answer_text"] = await wait_for(answer_present, 30, "the answer 345 in the transcript")

        log("== shutdown: finalize transcript + daily memory ==")
        await manager.shutdown()
        manager_shutdown = True
        await asyncio.sleep(1)

        text = transcript_text()
        results["transcript_written"] = bool(text) and "**user**" in text and "**assistant**" in text
        log("---- transcript ----")
        log(text or "  (empty)")
        log("--------------------")

        transcript_root = transcript_dir.resolve()
        daily_hits = [
            path
            for path in paths.storage_root.rglob("*.md")
            if transcript_root not in path.resolve().parents
            and "Joined a voice call" in path.read_text(encoding="utf-8")
        ]
        results["daily_memory"] = bool(daily_hits)
        if daily_hits:
            log(f"  daily memory entry: {daily_hits[0]}")
    finally:
        if not manager_shutdown:
            await manager.shutdown()
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task
        await caller_room.disconnect()
        await bot_client.close()

    log("\n==== RESULTS ====")
    for name, ok in results.items():
        log(f"  {'PASS' if ok else 'FAIL'}: {name}")
    all_ok = all(results.values())
    log("\nRESULT: " + ("PASS - full agent-speaking call loop works end to end" if all_ok else "FAIL"))
    log(f"(storage kept at {storage})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
