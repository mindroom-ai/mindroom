#!/usr/bin/env python3
"""Full live agent-speaking call test against prod mindroom.chat.

The complete voice-call loop with the REAL code path and a REAL OpenAI key:

  1. A synthetic or existing caller starts an Element Call in a fresh private
     room and connects to the deployed LiveKit SFU with a microphone track.
  2. The real CallManager (real nio client, real build_call_tools with a
     calculator toolkit, selected realtime or cascaded backend) joins the call.
  3. The caller hears the agent's spoken greeting (audio-energy check).
  4. The caller speaks a question (OpenAI TTS audio pushed into its mic
     track) asking the agent to multiply 15 by 23 with its calculator tool.
  5. The agent must call the `multiply` tool and speak the answer.
  6. On shutdown the transcript file and the agent's memory reference
     must exist and record the turns + tool use.

Register disposable accounts:
  MINDROOM_REG_TOKEN=... OPENAI_API_KEY=sk-... \
  SSL_CERT_FILE=$(python -c 'import certifi;print(certifi.where())') \
  .venv/bin/python tests/manual/scratch/live_agent_speaking_test.py

Reuse existing accounts:
  CALLER_USER_ID=... CALLER_DEVICE_ID=... CALLER_TOKEN=... \
  BOT_USER_ID=... BOT_DEVICE_ID=... BOT_TOKEN=... \
  OPENAI_API_KEY=sk-... \
  SSL_CERT_FILE=$(python -c 'import certifi;print(certifi.where())') \
  .venv/bin/python tests/manual/scratch/live_agent_speaking_test.py

Set ``MINDROOM_CALL_BACKEND=cascaded`` to exercise the cascaded backend; the default is realtime.
"""

from __future__ import annotations

import array
import asyncio
import base64
import io
import os
import secrets
import ssl
import sys
import tempfile
import uuid
import wave
from dataclasses import dataclass, field
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
from mindroom.config.auth import AuthorizationConfig  # noqa: E402
from mindroom.config.calls import CallsConfig, CascadedCallProfile, RealtimeCallProfile  # noqa: E402
from mindroom.config.main import Config  # noqa: E402
from mindroom.config.memory import MemoryConfig  # noqa: E402
from mindroom.config.models import ModelConfig  # noqa: E402
from mindroom.config.voice import SpeechServiceConfig  # noqa: E402
from mindroom.constants import resolve_runtime_paths  # noqa: E402
from mindroom.credentials_sync import sync_env_to_credentials  # noqa: E402
from mindroom.matrix.state import MatrixState  # noqa: E402
from mindroom.matrix_rtc import call_manager as cm  # noqa: E402
from mindroom.matrix_rtc.events import (  # noqa: E402
    CALL_MEMBER_EVENT_TYPE,
    build_membership_content,
    membership_state_key,
)
from mindroom.matrix_rtc.focus import OpenIDToken, request_sfu_grant  # noqa: E402
from mindroom.runtime_env_policy import CREDENTIALS_ENCRYPTION_KEY_ENV  # noqa: E402
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context  # noqa: E402
from mindroom.tool_system.worker_routing import agent_workspace_root_path, build_tool_execution_identity  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.message_target import MessageTarget
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

HOMESERVER = "https://mindroom.chat"
SERVICE_URL = "https://mindroom.chat/livekit/jwt"
CALL_BACKEND = os.environ.get("MINDROOM_CALL_BACKEND", "realtime").strip()
SUFFIX = uuid.uuid4().hex[:8]
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
AGENT = "assistant"
_CALL_MEMBERSHIP_EXPIRES_MS = 10 * 60 * 1000
_EXISTING_ACCOUNT_ENV_NAMES = (
    "CALLER_USER_ID",
    "CALLER_DEVICE_ID",
    "CALLER_TOKEN",
    "BOT_USER_ID",
    "BOT_DEVICE_ID",
    "BOT_TOKEN",
)

QUESTION = "Hi assistant! Please use your calculator tool to multiply fifteen by twenty three, and tell me the result."

SAMPLE_RATE = 24000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
VOICED_RMS = 250.0

if CALL_BACKEND not in {"realtime", "cascaded"}:
    msg = f"Unsupported MINDROOM_CALL_BACKEND: {CALL_BACKEND}"
    raise ValueError(msg)


@dataclass(frozen=True)
class AccountCredentials:
    """One existing or disposable Matrix account used by the live harness."""

    user_id: str
    device_id: str
    access_token: str = field(repr=False)


def log(m: str) -> None:
    """Print one unbuffered progress line."""
    sys.stdout.write(m + "\n")
    sys.stdout.flush()


def call_config(openai_key: str) -> CallsConfig:
    """Build the selected real call backend for this live harness."""
    if CALL_BACKEND == "realtime":
        return CallsConfig(
            enabled=True,
            profiles={
                "voice": RealtimeCallProfile(
                    backend="realtime",
                    model="gpt-realtime-2.1",
                    credentials_service="openai",
                    voice="marin",
                ),
            },
            agents={AGENT: "voice"},
            livekit_service_url=SERVICE_URL,
        )
    return CallsConfig(
        enabled=True,
        profiles={
            "voice": CascadedCallProfile(
                backend="cascaded",
                stt=SpeechServiceConfig(
                    provider="openai",
                    model="gpt-4o-transcribe",
                    api_key=openai_key,
                ),
                tts=SpeechServiceConfig(
                    provider="openai",
                    model="tts-1",
                    api_key=openai_key,
                    extra_kwargs={"voice": "alloy"},
                ),
            ),
        },
        agents={AGENT: "voice"},
        livekit_service_url=SERVICE_URL,
    )


async def register(localpart: str, registration_token: str) -> AccountCredentials:
    """Token-register one throwaway Matrix account."""
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
                "auth": {
                    "type": "m.login.registration_token",
                    "token": registration_token,
                    "session": session,
                },
            },
        )
    b = r.json()
    if not all(isinstance(b.get(field), str) for field in ("user_id", "device_id", "access_token")):
        msg = f"registration did not complete (status={r.status_code}, body={b})"
        raise TypeError(msg)
    return AccountCredentials(user_id=b["user_id"], device_id=b["device_id"], access_token=b["access_token"])


def existing_accounts_from_env() -> tuple[AccountCredentials, AccountCredentials] | None:
    """Load existing caller and bot accounts, rejecting partial configuration."""
    values = {name: os.environ.get(name, "").strip() for name in _EXISTING_ACCOUNT_ENV_NAMES}
    configured = [name for name, value in values.items() if value]
    if not configured:
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        msg = f"Existing-account mode requires all six account variables; missing: {', '.join(missing)}"
        raise RuntimeError(msg)
    return (
        AccountCredentials(values["CALLER_USER_ID"], values["CALLER_DEVICE_ID"], values["CALLER_TOKEN"]),
        AccountCredentials(values["BOT_USER_ID"], values["BOT_DEVICE_ID"], values["BOT_TOKEN"]),
    )


async def load_accounts() -> tuple[AccountCredentials, AccountCredentials]:
    """Use complete existing credentials or register two disposable accounts."""
    if accounts := existing_accounts_from_env():
        log("== using existing accounts from env ==")
        return accounts
    registration_token = os.environ.get("MINDROOM_REG_TOKEN", "").strip()
    if not registration_token:
        msg = "Set MINDROOM_REG_TOKEN or all six CALLER_*/BOT_* account variables"
        raise RuntimeError(msg)
    log("== register caller + bot ==")
    return (
        await register(f"speak_caller_{SUFFIX}", registration_token),
        await register(f"speak_bot_{SUFFIX}", registration_token),
    )


async def tts_pcm(text: str, openai_key: str) -> bytes:
    """Synthesize the caller's question to 24kHz mono PCM16."""
    async with httpx.AsyncClient(timeout=120.0) as h:
        r = await h.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {openai_key}"},
            json={"model": "tts-1", "voice": "alloy", "input": text, "response_format": "wav"},
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
        try:
            async for event in stream:
                samples = array.array("h", bytes(event.frame.data))
                if not samples:
                    continue
                rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                self.frames += 1
                self.peak_rms = max(self.peak_rms, rms)
                if rms > VOICED_RMS:
                    self.voiced += 1
        finally:
            await stream.aclose()


@dataclass
class CallerCall:
    """Caller-side LiveKit resources owned by one live test."""

    room: rtc.Room
    audio: CallerAudio
    meter: BotAudioMeter
    consumers: set[asyncio.Task[None]]
    pump_task: asyncio.Task[None]

    async def aclose(self) -> None:
        """Cancel media work, disconnect from the SFU, and close the audio source."""
        self.pump_task.cancel()
        await asyncio.gather(self.pump_task, return_exceptions=True)
        for task in self.consumers:
            task.cancel()
        if self.consumers:
            await asyncio.gather(*self.consumers, return_exceptions=True)
        try:
            await self.room.disconnect()
        finally:
            await self.audio.source.aclose()


async def caller_start_call(
    caller: AccountCredentials,
    room_id: str,
) -> CallerCall:
    """Publish the caller's call membership, connect to the SFU, publish a mic."""
    async with httpx.AsyncClient(base_url=HOMESERVER, timeout=30.0) as h:
        content = build_membership_content(
            user_id=caller.user_id,
            device_id=caller.device_id,
            livekit_service_url=SERVICE_URL,
            expires_ms=_CALL_MEMBERSHIP_EXPIRES_MS,
        )
        put = await h.put(
            f"/_matrix/client/v3/rooms/{room_id}/state/{CALL_MEMBER_EVENT_TYPE}/"
            f"{membership_state_key(caller.user_id, caller.device_id)}",
            headers={"Authorization": f"Bearer {caller.access_token}"},
            json=content,
        )
        put.raise_for_status()
        openid_response = await h.post(
            f"/_matrix/client/v3/user/{caller.user_id}/openid/request_token",
            headers={"Authorization": f"Bearer {caller.access_token}"},
            json={},
        )
        openid_response.raise_for_status()
        oid = openid_response.json()
    grant = await request_sfu_grant(
        SERVICE_URL,
        room_id=room_id,
        device_id=caller.device_id,
        openid_token=OpenIDToken(oid["access_token"], oid["expires_in"], oid["matrix_server_name"], oid["token_type"]),
    )
    room = rtc.Room()
    meter = BotAudioMeter()
    consumers: set[asyncio.Task[None]] = set()
    audio: CallerAudio | None = None

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, _pub: object, participant: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            log(f"  [caller] subscribed to audio from {participant.identity}")
            task = asyncio.create_task(meter.consume(track))
            consumers.add(task)
            task.add_done_callback(consumers.discard)

    try:
        await room.connect(grant.url, grant.jwt)
        audio = CallerAudio()
        mic = rtc.LocalAudioTrack.create_audio_track("mic", audio.source)
        await room.local_participant.publish_track(
            mic,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
    except BaseException:
        for task in consumers:
            task.cancel()
        if consumers:
            await asyncio.gather(*consumers, return_exceptions=True)
        try:
            await room.disconnect()
        finally:
            if audio is not None:
                await audio.source.aclose()
        raise
    log(f"  [caller] on SFU as {room.local_participant.identity}, mic published")
    return CallerCall(
        room=room,
        audio=audio,
        meter=meter,
        consumers=consumers,
        pump_task=asyncio.create_task(audio.pump(), name="matrix_rtc_live_test_caller_audio"),
    )


async def wait_for(predicate, timeout_s: float, what: str) -> bool:  # noqa: ANN001
    """Poll a predicate every 0.5s until true or the deadline passes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.5)
    log(f"  TIMEOUT waiting for {what}")
    return False


async def cleanup_matrix_room(
    room_id: str,
    caller: AccountCredentials,
    bot: AccountCredentials,
) -> bool:
    """Clear call state, then make both test accounts leave and forget the room."""
    cleanup_ok = True
    async with httpx.AsyncClient(base_url=HOMESERVER, timeout=30.0) as client:
        for account in (caller, bot):
            headers = {"Authorization": f"Bearer {account.access_token}"}
            operations = (
                (
                    "clear call membership",
                    "PUT",
                    f"/_matrix/client/v3/rooms/{room_id}/state/{CALL_MEMBER_EVENT_TYPE}/"
                    f"{membership_state_key(account.user_id, account.device_id)}",
                    {},
                ),
                ("leave room", "POST", f"/_matrix/client/v3/rooms/{room_id}/leave", {}),
                ("forget room", "POST", f"/_matrix/client/v3/rooms/{room_id}/forget", {}),
            )
            for label, method, url, body in operations:
                try:
                    response = await client.request(method, url, headers=headers, json=body)
                    response.raise_for_status()
                except httpx.HTTPError as error:
                    cleanup_ok = False
                    log(f"  cleanup failed for {account.user_id} ({label}): {error}")
    return cleanup_ok


def _ephemeral_encryption_key() -> str:
    """Return a process-local key so interrupted runs never leave plaintext provider credentials."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


async def main() -> int:  # noqa: C901, PLR0915
    """Run the full agent-speaking call loop and report PASS/FAIL per leg."""
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not openai_key:
        msg = "OPENAI_API_KEY is required"
        raise RuntimeError(msg)

    caller, bot = await load_accounts()
    log(f"  caller={caller.user_id} bot={bot.user_id}")

    log(f"== backend: {CALL_BACKEND} ==")
    log("== TTS: synthesizing the caller's question ==")
    question_pcm = await tts_pcm(QUESTION, openai_key)
    log(f"  {len(question_pcm) // 2 / SAMPLE_RATE:.1f}s of question audio ready")

    results: dict[str, bool] = {}
    room_id: str | None = None
    caller_call: CallerCall | None = None
    bot_client: nio.AsyncClient | None = None
    manager: cm.CallManager | None = None
    manager_shutdown = False

    with tempfile.TemporaryDirectory(prefix="speaktest_") as storage_dir:
        storage = Path(storage_dir)
        try:
            log("== create private room, bot joins ==")
            async with httpx.AsyncClient(base_url=HOMESERVER, timeout=30.0) as h:
                create_response = await h.post(
                    "/_matrix/client/v3/createRoom",
                    headers={"Authorization": f"Bearer {caller.access_token}"},
                    json={
                        "name": f"speaktest-{SUFFIX}",
                        "invite": [bot.user_id],
                        "preset": "private_chat",
                        "power_level_content_override": {"events": {CALL_MEMBER_EVENT_TYPE: 0}},
                    },
                )
                create_response.raise_for_status()
                room_id = create_response.json()["room_id"]
                join_response = await h.post(
                    f"/_matrix/client/v3/rooms/{room_id}/join",
                    headers={"Authorization": f"Bearer {bot.access_token}"},
                    json={},
                )
                join_response.raise_for_status()
            log(f"  room={room_id}")

            log("== caller starts the call (mic publishing silence) ==")
            caller_call = await caller_start_call(caller, room_id)
            meter = caller_call.meter

            log("== real CallManager joins with real key + real calculator tool ==")
            config_path = storage / "config.yaml"
            config_path.write_text("router:\n  model: default\n", encoding="utf-8")
            paths = resolve_runtime_paths(
                config_path=config_path,
                storage_path=storage / "mindroom_data",
                process_env={
                    "OPENAI_API_KEY": openai_key,
                    "MATRIX_HOMESERVER": HOMESERVER,
                    "MINDROOM_NAMESPACE": "",
                    CREDENTIALS_ENCRYPTION_KEY_ENV: _ephemeral_encryption_key(),
                },
            )
            sync_env_to_credentials(paths)
            # Entity resolution needs managed identities, but the live Matrix
            # token stays only on the client and is never persisted to state.
            state = MatrixState.load(paths)
            state.add_account("agent_router", f"speak_router_{SUFFIX}", "unused", domain="mindroom.chat")
            state.add_account(
                "agent_assistant",
                bot.user_id.split(":")[0].lstrip("@"),
                "unused",
                domain="mindroom.chat",
                device_id=bot.device_id,
            )
            state.save(paths)

            config = Config(
                authorization=AuthorizationConfig(room_permissions={room_id: [caller.user_id]}),
                agents={
                    AGENT: AgentConfig(
                        display_name="Assistant",
                        role="Helpful voice assistant",
                        tools=["calculator"],
                        rooms=[room_id],
                        memory_backend="file",
                    ),
                },
                models={"default": ModelConfig(provider="openai", id="gpt-5.6")},
                memory=MemoryConfig(backend="none"),
                calls=call_config(openai_key),
            )

            bot_client = nio.AsyncClient(HOMESERVER, bot.user_id, device_id=bot.device_id, ssl=SSL_CTX)
            bot_client.access_token = bot.access_token
            bot_client.user_id = bot.user_id
            bot_client.device_id = bot.device_id

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
                        requester_id=user_id or bot.user_id,
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
                        requester_id=user_id or bot.user_id,
                        room_id=target.room_id,
                        thread_id=target.source_thread_id,
                        resolved_thread_id=target.resolved_thread_id,
                        session_id=target.session_id,
                    )

                async def run_in_context(
                    self,
                    *,
                    tool_context: ToolRuntimeContext | None,
                    operation: Callable[[], Awaitable[str]],
                ) -> str:
                    """Mirror the production tool-support context binding."""
                    with tool_runtime_context(tool_context):
                        return await operation()

            manager = cm.CallManager(
                agent_name=AGENT,
                config=config,
                client=bot_client,
                runtime_paths=paths,
                ssl_verify=True,
                tool_support=LiveToolSupport(),  # type: ignore[arg-type]
                get_invited_rooms_by_agent=dict,
            )

            room_obj = nio.MatrixRoom(room_id=room_id, own_user_id=bot.user_id)
            room_obj.encrypted = False
            event = nio.UnknownEvent(
                {"event_id": "$evt", "sender": caller.user_id, "origin_server_ts": 1},
                CALL_MEMBER_EVENT_TYPE,
            )
            await asyncio.wait_for(manager.on_room_event(room_obj, event), timeout=120)
            log("  [bot] call join path completed (agent session started)")

            log("== leg 1: greeting audio from the agent ==")
            results["greeting_audio"] = await wait_for(lambda: meter.voiced >= 20, 45, "greeting audio")
            log(f"  bot audio: frames={meter.frames} voiced={meter.voiced} peak_rms={meter.peak_rms:.0f}")

            transcript_dir = agent_workspace_root_path(paths.storage_root, AGENT) / "calls"

            def transcript_text() -> str:
                files = list(transcript_dir.glob("*.md")) if transcript_dir.exists() else []
                return files[0].read_text(encoding="utf-8") if files else ""

            log("== leg 2: caller asks the calculator question ==")
            caller_call.audio.say(question_pcm)
            await wait_for(lambda: not caller_call.audio.speaking, 30, "question audio to finish playing")
            log("  question spoken; waiting for tool call + spoken answer")

            def answer_present() -> bool:
                text = transcript_text().lower().replace("-", " ")
                return "345" in text or ("three hundred" in text and "forty five" in text)

            results["tool_called"] = await wait_for(
                lambda: "tools used" in transcript_text() and "multiply" in transcript_text(),
                75,
                "tool use in transcript",
            )
            voiced_after_tool = meter.voiced
            results["answer_spoken"] = results["tool_called"] and await wait_for(
                lambda: meter.voiced > voiced_after_tool + 20,
                30,
                "answer audio after tool execution",
            )
            results["answer_text"] = await wait_for(answer_present, 30, "the answer 345 in the transcript")

            log("== shutdown: finalize transcript + memory reference ==")
            await manager.shutdown()
            manager_shutdown = True
            await asyncio.sleep(1)

            text = transcript_text()
            results["transcript_written"] = bool(text) and "**user**" in text and "**assistant**" in text
            log("---- transcript ----")
            log(text or "  (empty)")
            log("--------------------")

            transcript_root = transcript_dir.resolve()
            memory_hits = [
                path
                for path in paths.storage_root.rglob("*.md")
                if transcript_root not in path.resolve().parents
                and "Joined a voice call" in path.read_text(encoding="utf-8")
            ]
            results["memory_reference"] = bool(memory_hits)
            if memory_hits:
                log(f"  memory reference: {memory_hits[0]}")
        finally:
            cleanup_ok = True
            if manager is not None and not manager_shutdown:
                try:
                    await manager.shutdown()
                except Exception as error:
                    cleanup_ok = False
                    log(f"  CallManager cleanup failed: {error}")
            if caller_call is not None:
                try:
                    await caller_call.aclose()
                except Exception as error:
                    cleanup_ok = False
                    log(f"  caller media cleanup failed: {error}")
            if bot_client is not None:
                try:
                    await bot_client.close()
                except Exception as error:
                    cleanup_ok = False
                    log(f"  bot client cleanup failed: {error}")
            if room_id is not None:
                cleanup_ok = await cleanup_matrix_room(room_id, caller, bot) and cleanup_ok
            results["cleanup"] = cleanup_ok

    log("\n==== RESULTS ====")
    for name, ok in results.items():
        log(f"  {'PASS' if ok else 'FAIL'}: {name}")
    all_ok = all(results.values())
    log("\nRESULT: " + ("PASS - full agent-speaking call loop works end to end" if all_ok else "FAIL"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
