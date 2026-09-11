"""Exercise GPT-Live through the installed SDK with in-memory provider transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import aiohttp
import pytest
from livekit import rtc
from livekit.plugins.openai.realtime import GPTLiveSession

from mindroom.matrix_rtc.call_tools import CallAgentResponse
from mindroom.matrix_rtc.live_voice_agent import LiveVoiceBridge
from mindroom.matrix_rtc.voice_agent import LiveVoiceAgentOptions, RealtimeVoiceBridge

if TYPE_CHECKING:
    from collections.abc import Callable

    from livekit.agents import Agent, AgentSession
    from livekit.agents.llm import InputTranscriptionCompleted


class _ProviderSocket:
    """Model the server handshake and capture actual serialized client commands."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.closed_event = asyncio.Event()

    def feed(self, event: dict[str, Any]) -> None:
        """Deliver a raw provider event through the SDK's receive loop."""
        self.incoming.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(event), ""))

    async def receive(self) -> aiohttp.WSMessage:
        """Wait until the test or handshake produces another server event."""
        return await self.incoming.get()

    async def send_str(self, data: str) -> None:
        """Accept a serialized SDK command and answer lifecycle commands."""
        event = json.loads(data)
        self.sent.append(event)
        self.outgoing.put_nowait(event)
        if event["type"] == "session.start":
            self.feed({"type": "session.started", "session": {"id": "session-test"}})
        elif event["type"] == "session.close":
            self.feed({"type": "session.closed", "reason": "close_requested"})

    async def close(self) -> None:
        """Record that the SDK closed its socket."""
        self.closed = True
        self.closed_event.set()

    async def next_event(self, event_type: str) -> dict[str, Any]:
        """Wait briefly for one named command, ignoring unrelated commands."""
        async with asyncio.timeout(2):
            while True:
                event = await self.outgoing.get()
                if event["type"] == event_type:
                    return event


class _ProviderHTTP:
    """Replace the external HTTP client while preserving SDK client ownership."""

    def __init__(self) -> None:
        self.socket = _ProviderSocket()
        self.sockets = [self.socket]
        self.connected_again = asyncio.Event()
        self.closed = False
        self.connection: tuple[str, dict[str, str]] | None = None

    async def ws_connect(self, *, url: str, headers: dict[str, str]) -> _ProviderSocket:
        """Capture the endpoint and credentials selected by the real model."""
        if self.connection is not None:
            self.sockets.append(_ProviderSocket())
            self.connected_again.set()
        self.connection = (url, headers)
        return self.sockets[-1]

    async def close(self) -> None:
        """Record release of the model-owned HTTP client."""
        self.closed = True


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _ProviderHTTP:
    """Keep model/session logic real and replace external transport only."""
    client = _ProviderHTTP()
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: client)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://other-provider.example.org/v1")

    async def start_headless(
        bridge: RealtimeVoiceBridge,
        session: AgentSession,
        agent: Agent,
        options: LiveVoiceAgentOptions,
        **_kwargs: object,
    ) -> None:
        # The shared media bridge has separate room/roster tests. Run the actual
        # AgentSession here without an SFU or audio hardware.
        bridge._session = session
        bridge._register_session_listeners(session, options)
        await session.start(agent)

    monkeypatch.setattr(RealtimeVoiceBridge, "_start_session", start_headless)
    return client


@pytest.mark.asyncio
async def test_live_sdk_routes_delegation_and_closes_owned_work(provider: _ProviderHTTP) -> None:
    """Wrong delegation mode or listener wiring prevents a real provider-bound reply."""
    prompts: list[str] = []
    transcript: list[tuple[str, str]] = []
    order: list[str] = []
    second_started = asyncio.Event()

    async def respond(prompt: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        prompts.append(prompt)
        if len(prompts) == 1:
            return CallAgentResponse("The task is complete.")
        second_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            order.append("delegate_cancelled")
        return CallAgentResponse("unreachable")

    async def close_responder() -> None:
        order.append("responder_closed")

    system_prompt = "You are Helper. The caller is Alice.\n" + "Preserve every context section.\n" * 1200
    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value=system_prompt),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="vesper",
        respond=respond,
        close_responder=close_responder,
        on_conversation_turn=lambda speaker, text: transcript.append((speaker, text)),
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    room = SimpleNamespace(disconnect=AsyncMock())
    bridge._room = room
    try:
        await bridge.start_agent(options)
        start = await provider.socket.next_event("session.start")
        assert start["session"]["model"] == "gpt-live-1"
        assert start["session"]["audio"]["output"]["voice"] == "vesper"
        assert start["session"]["instructions"] == system_prompt
        assert start["session"]["delegation"] == {"type": "client"}
        assert provider.connection is not None
        url, headers = provider.connection
        assert url == "wss://api.openai.com/v1/live/sessions"
        assert headers["Authorization"] == "Bearer test-api-key"

        provider.socket.feed(
            {"type": "session.input_transcript.delta", "delta": "Check status.", "start_ms": 0, "end_ms": 100},
        )
        provider.socket.feed({"type": "session.delegation.created", "delegation": {"id": "first", "target": "client"}})
        reply = await provider.socket.next_event("session.commentary.append")
        assert reply["delegation_id"] == "first"
        assert reply["content"] == "The task is complete."
        assert "Check status." in prompts[0]

        provider.socket.feed(
            {"type": "session.input_transcript.delta", "delta": "Check again.", "start_ms": 2000, "end_ms": 2100},
        )
        provider.socket.feed({"type": "session.delegation.created", "delegation": {"id": "second", "target": "client"}})
        await asyncio.wait_for(second_started.wait(), timeout=2)
        assert ("user", "Check status.") in transcript
    finally:
        await asyncio.wait_for(bridge.aclose(), timeout=3)

    assert order == ["delegate_cancelled", "responder_closed"]
    assert provider.socket.closed
    assert provider.closed
    room.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize_retry", [False, True], ids=["pending-retry", "finalized-retry"])
async def test_live_sdk_identical_spoken_retry_survives_delegate_failure(
    provider: _ProviderHTTP,
    finalize_retry: bool,
) -> None:
    """A new utterance may repeat a failed request without losing its words."""
    prompts: list[str] = []
    transcript: list[tuple[str, str]] = []
    retry_transcribed = asyncio.Event()

    async def respond(prompt: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        prompts.append(prompt)
        if len(prompts) == 1:
            message = "delegated request failed"
            raise RuntimeError(message)
        return CallAgentResponse("Done.")

    def on_transcribed(event: InputTranscriptionCompleted) -> None:
        if not event.is_final:
            retry_transcribed.set()

    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="marin",
        respond=respond,
        on_conversation_turn=lambda speaker, text: transcript.append((speaker, text)),
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    try:
        await bridge.start_agent(options)
        await provider.socket.next_event("session.start")
        provider.socket.feed(
            {"type": "session.input_transcript.delta", "delta": "Send the update.", "start_ms": 0, "end_ms": 100},
        )
        provider.socket.feed({"type": "session.delegation.created", "delegation": {"id": "first", "target": "client"}})
        failure = await provider.socket.next_event("session.commentary.append")
        assert failure["delegation_id"] == "first"
        assert "could not complete" in failure["content"]

        agent = cast("Agent", bridge._live_agent)
        session = agent.duplex_session
        assert isinstance(session, GPTLiveSession)
        session.on("input_audio_transcription_completed", on_transcribed)
        # The gap makes the SDK finalize the original before starting the
        # identical retry. Both its transcript and delegation wiring stay real.
        provider.socket.feed(
            {"type": "session.input_transcript.delta", "delta": "Send the update.", "start_ms": 2000, "end_ms": 2100},
        )
        await asyncio.wait_for(retry_transcribed.wait(), timeout=2)
        if finalize_retry:
            session.push_audio(
                rtc.AudioFrame(data=bytes(48000), sample_rate=24000, num_channels=1, samples_per_channel=24000),
            )
        assert transcript.count(("user", "Send the update.")) == 1 + int(finalize_retry)
        provider.socket.feed({"type": "session.delegation.created", "delegation": {"id": "retry", "target": "client"}})
        reply = await provider.socket.next_event("session.commentary.append")

        assert reply["delegation_id"] == "retry"
        assert reply["content"] == "Done."
        assert len(prompts) == 2
        assert all(prompt.count("Send the update.") == 1 for prompt in prompts)
    finally:
        await asyncio.wait_for(bridge.aclose(), timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=["failed", "cancelled"])
async def test_live_prompt_preparation_failure_closes_responder_before_provider_start(
    provider: _ProviderHTTP,
    cancelled: bool,
) -> None:
    """Preparing the full prompt must not leak the cached agent on failed call startup."""
    error = asyncio.CancelledError if cancelled else RuntimeError
    close_responder = AsyncMock()
    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(side_effect=error("prompt preparation stopped")),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="marin",
        respond=AsyncMock(),
        close_responder=close_responder,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    try:
        with pytest.raises(error, match="prompt preparation stopped"):
            await bridge.start_agent(options)
    finally:
        await bridge.aclose()
    close_responder.assert_awaited_once()
    assert provider.connection is None


@pytest.mark.asyncio
async def test_live_sdk_configuration_failure_releases_owned_resources(
    provider: _ProviderHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected SDK startup must not retain its HTTP client or cached responder."""

    async def reject_configuration(_session: GPTLiveSession, _instructions: str) -> None:
        message = "provider configuration rejected"
        raise RuntimeError(message)

    monkeypatch.setattr(GPTLiveSession, "_update_instructions", reject_configuration)
    close_responder = AsyncMock()
    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="marin",
        respond=AsyncMock(),
        close_responder=close_responder,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    room = SimpleNamespace(disconnect=AsyncMock())
    bridge._room = room
    try:
        with pytest.raises(RuntimeError, match="provider configuration rejected"):
            await bridge.start_agent(options)
    finally:
        await asyncio.wait_for(bridge.aclose(), timeout=3)

    assert provider.socket.closed
    assert provider.closed
    close_responder.assert_awaited_once()
    room.disconnect.assert_awaited_once()
    assert bridge._session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("service_closed", [False, True], ids=["socket-loss", "session-expired"])
async def test_live_sdk_disconnect_returns_control_without_reusing_delegations(
    provider: _ProviderHTTP,
    service_closed: bool,
) -> None:
    """Reconnect must belong to the call manager, never reuse old provider delegation IDs."""
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    terminated = asyncio.Event()
    terminal_retries: list[bool] = []

    async def respond(_prompt: str, _on_tools: Callable[[list[str]], None] | None) -> CallAgentResponse:
        started.set()
        # A backend that finishes despite cancellation still cannot deliver
        # a result tagged with an obsolete provider delegation ID.
        with contextlib.suppress(asyncio.CancelledError):
            await release.wait()
        finished.set()
        return CallAgentResponse("Late result from the disconnected session.")

    def on_terminated(retryable: bool) -> None:
        terminal_retries.append(retryable)
        terminated.set()

    options = LiveVoiceAgentOptions(
        get_instructions=AsyncMock(return_value="Delegate requests."),
        model="gpt-live-1",
        api_key="test-api-key",
        voice="marin",
        respond=respond,
        on_session_terminated=on_terminated,
    )
    bridge = LiveVoiceBridge(local_identity="@bot:example.org:DEVICE", e2ee_enabled=False)
    bridge._room = SimpleNamespace(disconnect=AsyncMock())
    observers: list[asyncio.Task[bool]] = []
    try:
        await bridge.start_agent(options)
        await provider.socket.next_event("session.start")
        provider.socket.feed({"type": "session.input_transcript.delta", "delta": "Check status."})
        provider.socket.feed(
            {"type": "session.delegation.created", "delegation": {"id": "obsolete", "target": "client"}},
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        if service_closed:
            provider.socket.feed({"type": "session.closed", "reason": "expired"})
        provider.socket.incoming.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1000, ""))
        await asyncio.wait_for(provider.socket.closed_event.wait(), timeout=2)
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)

        observers = [asyncio.create_task(terminated.wait()), asyncio.create_task(provider.connected_again.wait())]
        await asyncio.wait(observers, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert terminated.is_set(), "SDK must report terminal disconnect before opening another provider session"
        assert terminal_retries == [True]
        assert len(provider.sockets) == 1
        assert not any(event.get("delegation_id") == "obsolete" for socket in provider.sockets for event in socket.sent)
    finally:
        release.set()
        for observer in observers:
            observer.cancel()
        await asyncio.gather(*observers, return_exceptions=True)
        await asyncio.wait_for(bridge.aclose(), timeout=3)
